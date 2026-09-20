"""
核心撮合业务测试：限价/市价、部分成交、撤单、幂等、手续费与舍入。
（单线程确定性逻辑，SQLite / MySQL 均可运行）
"""
from decimal import Decimal as D

from accounts.models import Account
from accounts.reconciliation import reconcile_snapshot
from trading.engine import bootstrap_order_book, engine
from trading.exceptions import (
    IdempotencyConflict,
    MaintenanceActive,
    OrderNotOpen,
)
from trading.models import Order, Trade

from .base import BaseEngineTestCase


class MatchingTests(BaseEngineTestCase):

    def assertConserved(self):
        result = reconcile_snapshot()
        self.assertTrue(result["ok"], result["mismatches"])

    # ---------- 部分成交 ----------
    def test_partial_fill_and_resting(self):
        # alice 挂卖 3 @ 50000
        sell, _ = engine.submit_order(
            user=self.alice, pair=self.pair, side="SELL", order_type="LIMIT",
            quantity=D("3"), limit_price=D("50000"),
        )
        # bob 买 1 -> alice 部分成交，剩 2 留在簿中
        buy, _ = engine.submit_order(
            user=self.bob, pair=self.pair, side="BUY", order_type="LIMIT",
            quantity=D("1"), limit_price=D("50000"),
        )
        sell.refresh_from_db()
        self.assertEqual(buy.status, Order.Status.FILLED)
        self.assertEqual(sell.status, Order.Status.PARTIALLY_FILLED)
        self.assertEqual(sell.filled_qty, D("1"))
        self.assertEqual(sell.remaining_frozen, D("2"))

        snap = engine.book(self.pair.pk).snapshot()
        self.assertEqual(len(snap["asks"]), 1)
        self.assertEqual(snap["asks"][0]["order_ids"], [sell.pk])

        # bob 收到 1 BTC 扣 0.1% -> 0.999
        bob_btc = self.account(self.bob, self.btc)
        self.assertEqual(bob_btc.available - D("100000"), D("0.999"))
        # alice 收到 50000 USDT 扣 0.1% -> 49950
        alice_usdt = self.account(self.alice, self.usdt)
        self.assertEqual(alice_usdt.available - D("100000000"), D("49950"))
        self.assertConserved()

    def test_price_priority_crosses_best_ask_first(self):
        # 两笔卖单：alice 51000 后挂，bob 50000 先挂
        a, _ = engine.submit_order(
            user=self.alice, pair=self.pair, side="SELL", order_type="LIMIT",
            quantity=D("1"), limit_price=D("51000"))
        b, _ = engine.submit_order(
            user=self.bob, pair=self.pair, side="SELL", order_type="LIMIT",
            quantity=D("1"), limit_price=D("50000"))
        # carol 市价买预算 50000，恰好买 1 BTC，应与更优价 50000（bob）成交
        m, _ = engine.submit_order(
            user=self.carol, pair=self.pair, side="BUY", order_type="MARKET",
            quantity=D("50000"))
        trade = Trade.objects.get(taker_order=m)
        self.assertEqual(trade.price, D("50000"))
        self.assertEqual(trade.maker_order_id, b.pk)

    def test_time_priority_same_price_fifo(self):
        ids = []
        for i, u in enumerate((self.alice, self.bob, self.carol)):
            o, _ = engine.submit_order(
                user=u, pair=self.pair, side="SELL", order_type="LIMIT",
                quantity=D("1"), limit_price=D("50000"), idem_key=f"s{i}")
            ids.append(o.pk)
        # 逐个吃单，必须严格按 id 升序；预算恰好覆盖 1 BTC @50000
        for expected in ids:
            m, _ = engine.submit_order(
                user=self.alice, pair=self.pair, side="BUY", order_type="MARKET",
                quantity=D("50000"), idem_key=f"m-{expected}")
            self.assertEqual(Trade.objects.get(taker_order=m).maker_order_id,
                             expected)

    # ---------- 市价单 ----------
    def test_market_sell(self):
        engine.submit_order(
            user=self.alice, pair=self.pair, side="BUY", order_type="LIMIT",
            quantity=D("2"), limit_price=D("50000"), idem_key="bid")
        m, _ = engine.submit_order(
            user=self.bob, pair=self.pair, side="SELL", order_type="MARKET",
            quantity=D("2"))
        self.assertEqual(m.status, Order.Status.FILLED)
        self.assertEqual(m.filled_qty, D("2"))

    def test_market_buy_remainder_cancels_and_refunds(self):
        # 簿上只有 1 BTC @50000，bob 用 90000 USDT 市价买
        engine.submit_order(
            user=self.alice, pair=self.pair, side="SELL", order_type="LIMIT",
            quantity=D("1"), limit_price=D("50000"), idem_key="ask")
        before = self.account(self.bob, self.usdt).available
        m, _ = engine.submit_order(
            user=self.bob, pair=self.pair, side="BUY", order_type="MARKET",
            quantity=D("90000"))
        m.refresh_from_db()
        # 成交 1 BTC 花 50000；市价单剩余立即取消，冻结全部释放
        self.assertEqual(m.status, Order.Status.CANCELED)
        self.assertEqual(m.filled_qty, D("1"))
        self.assertEqual(m.remaining_frozen, D("0"))
        after = self.account(self.bob, self.usdt).available
        self.assertEqual(before - after, D("50000"))  # 恰好花掉 50000
        self.assertConserved()

    def test_market_buy_budget_too_small_buys_nothing(self):
        engine.submit_order(
            user=self.alice, pair=self.pair, side="SELL", order_type="LIMIT",
            quantity=D("1"), limit_price=D("50000"), idem_key="ask")
        before = self.account(self.bob, self.usdt).available
        # 预算 10 USDT 连 1 lot(0.000001 BTC=0.05 USDT) 其实够；改成 0.001
        m, _ = engine.submit_order(
            user=self.bob, pair=self.pair, side="BUY", order_type="MARKET",
            quantity=D("0.001"))
        m.refresh_from_db()
        self.assertEqual(m.status, Order.Status.CANCELED)
        self.assertEqual(m.filled_qty, D("0"))
        after = self.account(self.bob, self.usdt).available
        self.assertEqual(before, after)  # 全额退回

    # ---------- 撤单 ----------
    def test_cancel_releases_freeze(self):
        o, _ = engine.submit_order(
            user=self.alice, pair=self.pair, side="BUY", order_type="LIMIT",
            quantity=D("2"), limit_price=D("50000"), idem_key="c1")
        acc = self.account(self.alice, self.usdt)
        self.assertEqual(acc.frozen, D("100000"))
        canceled = engine.cancel_order(user=self.alice, order_id=o.pk)
        self.assertEqual(canceled.status, Order.Status.CANCELED)
        acc.refresh_from_db()
        self.assertEqual(acc.frozen, D("0"))
        self.assertEqual(acc.available, D("100000000"))
        self.assertConserved()

    def test_cancel_filled_order_conflicts(self):
        o, _ = engine.submit_order(
            user=self.alice, pair=self.pair, side="SELL", order_type="LIMIT",
            quantity=D("1"), limit_price=D("50000"), idem_key="x1")
        engine.submit_order(
            user=self.bob, pair=self.pair, side="BUY", order_type="LIMIT",
            quantity=D("1"), limit_price=D("50000"), idem_key="x2")
        with self.assertRaises(OrderNotOpen):
            engine.cancel_order(user=self.alice, order_id=o.pk)

    def test_cancel_other_user_order_404(self):
        from trading.exceptions import OrderNotFound

        o, _ = engine.submit_order(
            user=self.alice, pair=self.pair, side="BUY", order_type="LIMIT",
            quantity=D("1"), limit_price=D("50000"), idem_key="own")
        with self.assertRaises(OrderNotFound):
            engine.cancel_order(user=self.bob, order_id=o.pk)

    # ---------- 幂等 ----------
    def test_idempotency_repeat_no_duplicate(self):
        kwargs = dict(user=self.alice, pair=self.pair, side="BUY",
                      order_type="LIMIT", quantity=D("1"),
                      limit_price=D("50000"))
        o1, c1 = engine.submit_order(idem_key="K1", **kwargs)
        o2, c2 = engine.submit_order(idem_key="K1", **kwargs)
        self.assertEqual(o1.pk, o2.pk)
        self.assertTrue(c1)
        self.assertFalse(c2)
        self.assertEqual(Order.objects.filter(idem_keys__key="K1").count(), 1)

    def test_idempotency_same_key_different_params_conflict(self):
        engine.submit_order(
            user=self.alice, pair=self.pair, side="BUY", order_type="LIMIT",
            quantity=D("1"), limit_price=D("50000"), idem_key="K2")
        with self.assertRaises(IdempotencyConflict):
            engine.submit_order(
                user=self.alice, pair=self.pair, side="BUY", order_type="LIMIT",
                quantity=D("2"), limit_price=D("50000"), idem_key="K2")

    # ---------- 余额不足与参数 ----------
    def test_insufficient_balance_rejected(self):
        from accounts.services import BalanceError

        with self.assertRaises(BalanceError):
            engine.submit_order(
                user=self.alice, pair=self.pair, side="BUY", order_type="LIMIT",
                quantity=D("100000"), limit_price=D("50000"), idem_key="poor")

    def test_tick_lot_validation(self):
        with self.assertRaises(ValueError):
            engine.submit_order(
                user=self.alice, pair=self.pair, side="BUY", order_type="LIMIT",
                quantity=D("1"), limit_price=D("50000.001"), idem_key="tick")
        with self.assertRaises(ValueError):
            engine.submit_order(
                user=self.alice, pair=self.pair, side="SELL", order_type="LIMIT",
                quantity=D("0.0000001"), limit_price=D("50000"), idem_key="lot")

    def test_no_cross_when_prices_do_not_overlap(self):
        engine.submit_order(
            user=self.alice, pair=self.pair, side="SELL", order_type="LIMIT",
            quantity=D("1"), limit_price=D("51000"), idem_key="ask")
        o, _ = engine.submit_order(
            user=self.bob, pair=self.pair, side="BUY", order_type="LIMIT",
            quantity=D("1"), limit_price=D("50000"), idem_key="bid")
        self.assertEqual(o.status, Order.Status.NEW)
        self.assertEqual(Trade.objects.count(), 0)
