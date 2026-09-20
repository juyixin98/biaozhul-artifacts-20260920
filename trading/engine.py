"""
本地模拟撮合引擎（进程内单例）。

并发模型
--------
- 进程内每个交易对一把 RLock（pair_locks）。同一交易对的下单/成交/撤单/
  撤光全部串行，因此成交与撤单的竞争只有一种合法结果：先拿到锁的一方生效，
  另一方看到终态订单后得到明确结论（已成交/已撤销）。
- 进程内多线程（gthread worker）共享内存订单簿；只部署 1 个 worker 进程。
- 纵深防御：每笔成交与撤单在数据库事务中对订单行 select_for_update，
  并再次校验状态；任何余额变动都在账户行锁下完成。
- 维护模式标志为布尔值，CPython 下读取原子，直接读不持锁；
  写标志持 config_lock，避免与“撤光”流程自身交错，也避免锁序反转死锁。

崩溃/恢复
----------
- 每笔成交是一个独立数据库事务：账户余额、双方订单剩余量、Trade 行、
  复式账本分录要么全部提交、要么全部回滚。内存簿只在提交成功后更新，
  因此成交中断（进程在事务提交前后崩溃）都不会重复结算。
- 启动时 bootstrap_order_book() 从 NEW/PARTIALLY_FILLED 订单按
  (side, limit_price, id) 重建内存簿，恢复原有价格-时间顺序。

舍入与冻结（关键）
------------------
- 下单冻结对“限价*数量”做 ROUND_CEILING（多冻不超过 1e-8 的零头）。
- 成交金额 quote_amount = floor8(price * qty)：8 位精度向下截断。
  任意多笔成交金额之和都不超过“限价*总量”，绝不会击穿买方冻结；
  被截断的零头（< 1e-8/笔）留在买方剩余冻结里，订单终结时退回买方可用，
  每笔分录内部 quote 流出=卖方收到+手续费，全局资产守恒。
- 手续费从“收到的资产”扣除，ROUND_FLOOR，最少为 0。
"""
import hashlib
import json
import threading
from decimal import Decimal

from django.db import IntegrityError, transaction

from accounts.models import Account, Asset
from accounts.services import (
    freeze,
    get_or_create_user_account,
    get_system_account,
    settle_fill,
    unfreeze,
)

from .decimal_utils import (
    D,
    ZERO,
    apply_lot,
    apply_tick,
    ceil8,
    floor8,
    quantize,
)
from .exceptions import (
    IdempotencyConflict,
    MaintenanceActive,
    OrderNotOpen,
    OrderNotFound,
)
from .models import AuditLog, IdempotencyKey, Order, SystemConfig, Trade, TradingPair
from .orderbook import OrderBook


def _audit(actor, action: str, target: str = "", detail: str = "") -> None:
    AuditLog.objects.create(
        actor=actor, action=action, target=target, detail=detail[:4000]
    )


class MatchingEngine:
    def __init__(self):
        self._books: dict[int, OrderBook] = {}
        self._pair_locks: dict[int, threading.RLock] = {}
        self._book_guard = threading.Lock()
        self._config_lock = threading.RLock()

    # ---------- 簿与锁 ----------
    def book(self, pair_id: int) -> OrderBook:
        with self._book_guard:
            if pair_id not in self._books:
                self._books[pair_id] = OrderBook(pair_id)
            return self._books[pair_id]

    def lock_pair(self, pair_id: int) -> threading.RLock:
        with self._book_guard:
            if pair_id not in self._pair_locks:
                self._pair_locks[pair_id] = threading.RLock()
            return self._pair_locks[pair_id]

    def reset_for_tests(self) -> None:
        """测试辅助：清空全部内存簿。"""
        with self._book_guard:
            self._books.clear()

    @staticmethod
    def _maintenance_on() -> bool:
        # 单行配置读取；布尔值读取在 CPython 下原子，无需持锁
        return SystemConfig.load().maintenance_mode

    # ---------- 启动恢复 ----------
    def bootstrap(self) -> dict:
        """从数据库重建所有启用交易对的订单簿。重复调用安全。"""
        with self._book_guard:
            self._books.clear()
            for pair in TradingPair.objects.filter(is_active=True):
                self._books[pair.pk] = OrderBook(pair.pk)

        result = {}
        for pair in TradingPair.objects.filter(is_active=True).order_by("id"):
            # 同价档位 deque 必须保持 id 升序：按 (limit_price, id) 排序恢复，
            # 价格优先由堆维护，同价 FIFO 由此保证。
            open_orders = list(
                Order.objects.filter(
                    pair_id=pair.pk,
                    status__in=[Order.Status.NEW, Order.Status.PARTIALLY_FILLED],
                ).order_by("limit_price", "id")
            )
            self.book(pair.pk).restore(open_orders)
            result[pair.symbol] = len(open_orders)
        return result

    # ================= 下单 =================
    def submit_order(
        self,
        *,
        user,
        pair: TradingPair,
        side: str,
        order_type: str,
        quantity: Decimal,
        limit_price: Decimal = None,
        idem_key: str = None,
    ) -> tuple[Order, bool]:
        """提交订单，返回 (order, created)；幂等重复提交 created=False。"""
        if self._maintenance_on():
            raise MaintenanceActive("系统维护中，暂停接单")

        limit_price_n, quantity_n = self._validate_params(
            pair, side, order_type, quantity, limit_price
        )
        fingerprint = self._fingerprint(
            pair.pk, side, order_type, str(quantity_n),
            str(limit_price_n) if limit_price_n is not None else None,
        )

        if idem_key:
            existing = IdempotencyKey.objects.filter(
                user=user, key=idem_key
            ).select_related("order").first()
            if existing is not None:
                self._check_fingerprint(user, idem_key, existing.fingerprint,
                                        fingerprint)
                return existing.order, False

        pair_lock = self.lock_pair(pair.pk)
        with pair_lock:
            # 拿锁后复查维护标志（enter_maintenance 置标志后才取 pair 锁）
            if self._maintenance_on():
                raise MaintenanceActive("系统维护中，暂停接单")

            if idem_key:
                existing = IdempotencyKey.objects.filter(
                    user=user, key=idem_key
                ).select_related("order").first()
                if existing is not None:
                    self._check_fingerprint(user, idem_key, existing.fingerprint,
                                            fingerprint)
                    return existing.order, False

            base, quote = pair.base, pair.quote
            if side == "BUY":
                freeze_asset = quote
                need_freeze = (
                    ceil8(limit_price_n * quantity_n)
                    if order_type == "LIMIT"
                    else quantize(quantity_n)  # MARKET 买：quantity 是 quote 预算
                )
            else:
                freeze_asset = base
                need_freeze = quantize(quantity_n)  # 限价卖/市价卖冻 base 数量

            account = get_or_create_user_account(user, freeze_asset)

            try:
                with transaction.atomic():
                    freeze(account, need_freeze)
                    order = Order.objects.create(
                        user=user,
                        pair=pair,
                        side=side,
                        type=order_type,
                        limit_price=limit_price_n,
                        orig_qty=quantity_n,
                        remaining_frozen=need_freeze,
                        account=account,
                        status=Order.Status.NEW,
                    )
                    if idem_key:
                        # pair 锁串行同交易对；不同交易对共用键时唯一约束兜底
                        IdempotencyKey.objects.create(
                            user=user, key=idem_key, order=order,
                            fingerprint=fingerprint,
                        )
            except IntegrityError:
                # 冻结与订单已随事务回滚；按幂等语义重新判定
                existing = IdempotencyKey.objects.get(user=user, key=idem_key)
                self._check_fingerprint(user, idem_key, existing.fingerprint,
                                        fingerprint)
                return existing.order, False

            # 撮合；任何意外都兜底撤单释放冻结，避免孤儿资金
            try:
                self._match(order, pair, base, quote)
            except Exception:
                self._safe_abort_order(order)
                raise

            order.refresh_from_db()
            return order, True

    @staticmethod
    def _check_fingerprint(user, idem_key, saved, incoming):
        if saved != incoming:
            _audit(user, "IDEM_CONFLICT", idem_key,
                   f"saved={saved} incoming={incoming}")
            raise IdempotencyConflict("相同幂等键对应不同的下单参数")

    def _safe_abort_order(self, order: Order):
        """撮合异常后的兜底：尽力撤单并释放全部冻结。"""
        try:
            with transaction.atomic():
                locked = Order.objects.select_for_update().filter(
                    pk=order.pk
                ).first()
                if locked and locked.is_open:
                    acc = Account.objects.select_for_update().get(
                        pk=locked.account_id
                    )
                    if locked.remaining_frozen > 0:
                        unfreeze(acc, locked.remaining_frozen)
                    locked.remaining_frozen = ZERO
                    locked.status = Order.Status.CANCELED
                    locked.save()
        except Exception:  # noqa: BLE001
            pass

    # ---------- 参数校验 ----------
    @staticmethod
    def _validate_params(pair, side, order_type, quantity, limit_price):
        if side not in ("BUY", "SELL"):
            raise ValueError("方向必须是 BUY 或 SELL")
        quantity = D(quantity)
        if quantity <= 0:
            raise ValueError("数量/预算必须大于 0")
        if order_type == "LIMIT":
            if limit_price is None:
                raise ValueError("限价单必须提供价格")
            limit_price = apply_tick(D(limit_price), pair.tick_size)
            quantity = apply_lot(quantity, pair.lot_size)
            if limit_price * quantity < pair.min_notional:
                raise ValueError("下单金额低于最小下单金额")
        else:
            limit_price = None
            if side == "SELL":
                quantity = apply_lot(quantity, pair.lot_size)
            else:
                quantity = quantize(quantity)  # MARKET 买入预算按 8 位
        return limit_price, quantity

    @staticmethod
    def _fingerprint(pair_id, side, order_type, quantity, limit_price) -> str:
        raw = json.dumps(
            {"p": pair_id, "s": side, "t": order_type,
             "q": quantity, "l": limit_price},
            sort_keys=True,
        )
        return hashlib.sha256(raw.encode()).hexdigest()

    # ================= 撮合主循环 =================
    def _match(self, taker: Order, pair: TradingPair, base: Asset, quote: Asset):
        book = self.book(pair.pk)
        cfg = SystemConfig.load()

        def can_cross(opp_price: Decimal) -> bool:
            if taker.type == "MARKET":
                return True
            return (
                taker.limit_price >= opp_price
                if taker.side == "BUY"
                else taker.limit_price <= opp_price
            )

        while True:
            price, level = book.best_ask() if taker.side == "BUY" else book.best_bid()
            if price is None or not can_cross(price):
                break
            maker_id = level[0]
            result = self._try_fill(
                taker=taker, maker_id=maker_id, pair=pair,
                base=base, quote=quote, book=book, cfg=cfg,
            )
            if result == "maker_invalid":
                # 惰性删除已标记，下一轮 best_* 会弹出该档位队首
                continue
            _maker_done, taker_done = result
            if taker_done:
                break

        taker.refresh_from_db()
        if taker.status in (Order.Status.NEW, Order.Status.PARTIALLY_FILLED):
            if taker.type == "LIMIT":
                self._finalize_resting_limit(taker, book)
            else:
                self._cancel_market_remainder(taker, book)

    # ---------- 单笔成交 ----------
    def _try_fill(self, *, taker, maker_id, pair, base, quote, book, cfg):
        """
        返回 (maker_done, taker_done)；maker 已完结则返回 "maker_invalid"。
        """
        with transaction.atomic():
            ids = sorted([taker.pk, maker_id])
            rows = list(
                Order.objects.select_for_update().filter(pk__in=ids).order_by("pk")
            )
            locked = {o.pk: o for o in rows}
            taker_l = locked[taker.pk]
            maker_l = locked[maker_id]

            if maker_l.status not in (
                Order.Status.NEW,
                Order.Status.PARTIALLY_FILLED,
            ):
                book.remove(maker_id, maker_l.side, maker_l.limit_price)
                return "maker_invalid"
            if not taker_l.is_open:
                return False, True  # 防御：taker 已完结，结束循环

            price = maker_l.limit_price  # 价格优先：以 maker 挂单价成交
            lot = pair.lot_size

            # 1) 候选数量
            if taker.side == "BUY" and taker.type == "MARKET":
                raw = taker_l.remaining_frozen / price
                qty = quantize((raw / lot).to_integral_value() * lot, lot)
            else:
                qty = taker_l.remaining_qty
            qty = quantize(min(qty, maker_l.remaining_qty), lot)
            if qty <= 0:
                # 市价买预算不足 1 个 lot：结束（剩余取消）
                return False, taker.type == "MARKET" and taker.side == "BUY"

            # 2) 成交金额：floor8 截断，保证买方逐笔累计不超冻结
            quote_amount = floor8(price * qty)

            # 3) taker 买方（仅市价买会跨档超预算；限价买由 CEILING 冻结保证）：
            #    若金额超出剩余冻结，逐 lot 收缩数量直至可负担
            if taker_l.side == "BUY" and quote_amount > taker_l.remaining_frozen:
                while qty > 0 and quote_amount > taker_l.remaining_frozen:
                    qty = quantize(qty - lot, lot)
                    quote_amount = floor8(price * qty)
                if qty <= 0:
                    # 市价买预算不足 1 个 lot：结束，剩余预算稍后退回
                    return False, True
            if qty <= 0 or quote_amount <= 0:
                return False, taker.type == "MARKET"

            # 4) 手续费（从收到的资产扣，FLOOR 向下，最少 0）
            if taker_l.side == "BUY":
                taker_fee = floor8(qty * D(cfg.taker_fee_rate))          # 收 base
                maker_fee = floor8(quote_amount * D(cfg.maker_fee_rate))  # 收 quote
            else:
                taker_fee = floor8(quote_amount * D(cfg.taker_fee_rate))  # 收 quote
                maker_fee = floor8(qty * D(cfg.maker_fee_rate))          # 收 base

            # 5) 账户（收到资产的账户可能尚不存在，get_or_create 后统一行锁）
            account_map = {}
            for uid in (taker_l.user_id, maker_l.user_id):
                for asset in (base, quote):
                    account_map[(uid, asset.pk)], _ = Account.objects.get_or_create(
                        user_id=uid,
                        asset_id=asset.pk,
                        defaults={"account_type": Account.AccountType.USER},
                    )
            fee_base = get_system_account(Account.AccountType.FEE, base)
            fee_quote = get_system_account(Account.AccountType.FEE, quote)

            trade = Trade.objects.create(
                pair=pair, taker_order=taker_l, maker_order=maker_l,
                price=price, quantity=qty, quote_amount=quote_amount,
                taker_fee=taker_fee, maker_fee=maker_fee,
            )
            settle_fill(
                taker=taker_l, maker=maker_l, accounts_by_key=account_map,
                base=base, quote=quote, base_qty=qty, quote_amount=quote_amount,
                taker_fee=taker_fee, maker_fee=maker_fee,
                fee_base_acc=fee_base, fee_quote_acc=fee_quote,
                ref_id=str(trade.pk),
            )

            # 6) 更新订单进度与冻结
            taker_l.filled_qty += qty
            taker_l.filled_quote += quote_amount
            maker_l.filled_qty += qty
            maker_l.filled_quote += quote_amount

            taker_l.remaining_frozen -= (
                quote_amount if taker_l.side == "BUY" else qty
            )
            maker_l.remaining_frozen -= (
                quote_amount if maker_l.side == "BUY" else qty
            )

            maker_done = maker_l.remaining_qty <= 0
            maker_l.status = (
                Order.Status.FILLED if maker_done else Order.Status.PARTIALLY_FILLED
            )

            taker_done = self._is_taker_done(taker_l)
            taker_l.status = (
                Order.Status.FILLED if taker_done else Order.Status.PARTIALLY_FILLED
            )

            # 订单全部成交时，CEILING 冻结与 FLOOR 成交额之间的粉尘必须退回，
            # 否则 FILLED 订单会永久占用冻结（部分成交的粉尘在重挂时退还）。
            for finished in (maker_l, taker_l):
                if (finished.status == Order.Status.FILLED
                        and finished.remaining_frozen > 0):
                    f_acc = Account.objects.select_for_update().get(
                        pk=finished.account_id
                    )
                    unfreeze(f_acc, finished.remaining_frozen)
                    finished.remaining_frozen = ZERO

            taker_l.save()
            maker_l.save()

            taker.filled_qty = taker_l.filled_qty
            taker.filled_quote = taker_l.filled_quote
            taker.remaining_frozen = taker_l.remaining_frozen
            taker.status = taker_l.status

        # ---- 事务提交成功后更新内存簿 ----
        if maker_done:
            book.remove(maker_id, maker_l.side, price)
            levels = book._ask_levels if maker_l.side == "SELL" else book._bid_levels
            dq = levels.get(price)
            if dq:
                dq.popleft()
        return maker_done, taker_done

    @staticmethod
    def _is_taker_done(taker: Order) -> bool:
        if taker.type == "LIMIT":
            return taker.remaining_qty <= 0
        if taker.side == "BUY":
            return taker.remaining_frozen <= 0  # 市价买预算耗尽
        return taker.remaining_qty <= 0

    # ---------- 限价单剩余挂簿 ----------
    def _finalize_resting_limit(self, order: Order, book: OrderBook):
        with transaction.atomic():
            locked = Order.objects.select_for_update().get(pk=order.pk)
            if not locked.is_open:
                return
            # 冻结按剩余委托重算，释放成交后多冻的零头（买单才有）
            if locked.side == "BUY":
                should_freeze = ceil8(locked.limit_price * locked.remaining_qty)
            else:
                should_freeze = locked.remaining_qty
            dust = locked.remaining_frozen - should_freeze
            if dust > 0:
                acc = Account.objects.select_for_update().get(pk=locked.account_id)
                unfreeze(acc, dust)
                locked.remaining_frozen = should_freeze
                locked.save(update_fields=["remaining_frozen"])
        if locked.is_open:
            book.add_resting(locked)

    # ---------- 市价单剩余取消 ----------
    def _cancel_market_remainder(self, order: Order, book: OrderBook):
        with transaction.atomic():
            locked = Order.objects.select_for_update().get(pk=order.pk)
            if not locked.is_open:
                return
            acc = Account.objects.select_for_update().get(pk=locked.account_id)
            if locked.remaining_frozen > 0:
                unfreeze(acc, locked.remaining_frozen)
            locked.remaining_frozen = ZERO
            locked.status = Order.Status.CANCELED
            locked.save()

    # ================= 撤单 =================
    def cancel_order(self, *, user, order_id: int) -> Order:
        """
        用户撤单。与成交在同一 pair 锁下串行：
        先提交 DB 撤单（锁行+状态机校验），成功后再惰性移出内存簿，
        因此“成交与撤单竞争”只会产生一种合法结果。
        """
        order = Order.objects.filter(pk=order_id).first()
        if order is None or order.user_id != user.id:
            raise OrderNotFound("订单不存在")
        with self.lock_pair(order.pair_id):
            return self._cancel_locked(
                order, user=user, detail="用户主动撤单", audit=True
            )

    def _cancel_locked(self, order: Order, *, user, detail: str, audit: bool):
        with transaction.atomic():
            locked = Order.objects.select_for_update().get(pk=order.pk)
            if locked.status == Order.Status.CANCELED:
                return locked  # 重复撤单幂等
            if locked.status == Order.Status.FILLED:
                raise OrderNotOpen("订单已全部成交，不能撤销")
            if not locked.is_open:
                raise OrderNotOpen(f"订单状态 {locked.status} 不可撤销")

            acc = Account.objects.select_for_update().get(pk=locked.account_id)
            if locked.remaining_frozen > 0:
                unfreeze(acc, locked.remaining_frozen)
            locked.remaining_frozen = ZERO
            locked.status = Order.Status.CANCELED
            locked.save()

        book = self.book(locked.pair_id)
        book.remove(locked.pk, locked.side, locked.limit_price)
        if audit:
            _audit(user, "ORDER_CANCEL", f"order:{locked.pk}", detail)
        return locked

    # ================= 维护模式 =================
    def enter_maintenance(self, *, actor) -> int:
        """
        进入维护模式：先置标志（新单立刻被拒），再按 pair_id 顺序逐簿加锁
        撤光所有挂单、释放冻结。重复进入安全（撤单幂等）。
        """
        with self._config_lock:
            cfg = SystemConfig.load()
            if cfg.maintenance_mode:
                return 0
            cfg.maintenance_mode = True
            cfg.save(update_fields=["maintenance_mode", "updated_at"])
            _audit(actor, "MAINTENANCE_ON", "system", "进入维护模式，停止接单")

        canceled = 0
        pair_ids = sorted(
            set(
                Order.objects.filter(
                    status__in=[Order.Status.NEW, Order.Status.PARTIALLY_FILLED]
                ).values_list("pair_id", flat=True)
            )
        )
        for pair_id in pair_ids:
            with self.lock_pair(pair_id):
                open_ids = list(
                    Order.objects.filter(
                        pair_id=pair_id,
                        status__in=[
                            Order.Status.NEW,
                            Order.Status.PARTIALLY_FILLED,
                        ],
                    ).order_by("id").values_list("id", flat=True)
                )
                for oid in open_ids:
                    order = Order.objects.get(pk=oid)
                    try:
                        self._cancel_locked(
                            order, user=actor, detail="维护模式批量撤单", audit=False
                        )
                        canceled += 1
                    except OrderNotOpen:
                        continue
        _audit(actor, "MAINTENANCE_DRAIN", "system",
               f"维护模式批量撤单完成，撤销 {canceled} 笔")
        return canceled

    def exit_maintenance(self, *, actor) -> None:
        with self._config_lock:
            cfg = SystemConfig.load()
            cfg.maintenance_mode = False
            cfg.save(update_fields=["maintenance_mode", "updated_at"])
            _audit(actor, "MAINTENANCE_OFF", "system", "退出维护模式，恢复接单")


# 进程内单例
engine = MatchingEngine()


def bootstrap_order_book() -> dict:
    """供 wsgi/测试调用：从持久化状态恢复内存簿。"""
    return engine.bootstrap()
