"""
并发测试：仅在 MySQL（支持行锁与多连接）下运行。

使用 TransactionTestCase（setUp 数据真实提交）+ 线程池，
模拟 gthread 多 worker 线程同时操作同一交易对。
"""
import threading
from decimal import Decimal as D

from django.contrib.auth.models import User
from django.db import connection
from django.test import TransactionTestCase

from accounts.models import Account
from accounts.reconciliation import reconcile_snapshot
from accounts.services import issue_simulated_asset
from trading.engine import bootstrap_order_book, engine
from trading.exceptions import OrderNotOpen
from trading.models import Asset, Order, SystemConfig, Trade, TradingPair

MYSQL = connection.vendor == "mysql"


def _thread(fn, *args, **kwargs):
    """在线程中执行 fn，并在线程结束时关闭它独占的数据库连接。"""
    box = {}

    def runner():
        try:
            fn(*args, **kwargs)
        except Exception as exc:  # noqa: BLE001
            import traceback

            box["error"] = repr(exc)
            box["tb"] = traceback.format_exc()
        finally:
            from django.db import connection as c

            c.close()

    t = threading.Thread(target=runner)
    t.start()
    return t, box


import unittest  # noqa: E402


@unittest.skipUnless(MYSQL, "并发测试需要 MySQL（行锁 + 多连接）")
class ConcurrentCancelVsFillTests(TransactionTestCase):

    def setUp(self):
        engine.reset_for_tests()
        self.usdt, _ = Asset.objects.get_or_create(symbol="USDT")
        self.btc, _ = Asset.objects.get_or_create(symbol="BTC")
        self.pair, _ = TradingPair.objects.get_or_create(
            symbol="BTCUSDT",
            defaults={
                "base": self.btc, "quote": self.usdt,
                "tick_size": D("0.01"), "lot_size": D("0.000001"),
                "min_notional": D("0"),
            },
        )
        self.alice = User.objects.create_user("alice_c", password="x")
        self.bob = User.objects.create_user("bob_c", password="x")
        from django.db import transaction as db_tx

        with db_tx.atomic():
            for u in (self.alice, self.bob):
                issue_simulated_asset(u, self.btc, D("100000"))
                issue_simulated_asset(u, self.usdt, D("100000000"))
        SystemConfig.load()
        bootstrap_order_book()

    def test_cancel_and_fill_race_single_legal_outcome(self):
        for i in range(5):
            with self.subTest(iteration=i):
                maker, _ = engine.submit_order(
                    user=self.alice, pair=self.pair, side="SELL",
                    order_type="LIMIT", quantity=D("1"),
                    limit_price=D("50000"), idem_key=f"maker-{i}")
                maker_id = maker.pk
                barrier = threading.Barrier(2)
                outcomes = {}

                def cancel():
                    barrier.wait()
                    try:
                        engine.cancel_order(user=self.alice, order_id=maker_id)
                        outcomes["cancel"] = "canceled"
                    except OrderNotOpen:
                        outcomes["cancel"] = "filled_first"

                def buy():
                    barrier.wait()
                    o, _ = engine.submit_order(
                        user=self.bob, pair=self.pair, side="BUY",
                        order_type="MARKET", quantity=D("60000"),
                        idem_key=f"buy-{i}")
                    o.refresh_from_db()
                    outcomes["buy_status"] = o.status
                    outcomes["buy_filled"] = o.filled_qty

                t1, e1 = _thread(cancel)
                t2, e2 = _thread(buy)
                t1.join(timeout=30)
                t2.join(timeout=30)
                self.assertIsNone(e1.get("error"), e1.get("tb"))
                self.assertIsNone(e2.get("error"), e2.get("tb"))

                maker.refresh_from_db()
                trades = Trade.objects.filter(maker_order_id=maker_id).count()

                if maker.status == Order.Status.FILLED:
                    # 成交先发生：撤单必须收到“已成交”，且只有 1 笔成交
                    self.assertEqual(outcomes["cancel"], "filled_first")
                    self.assertEqual(trades, 1)
                    self.assertEqual(outcomes["buy_status"], Order.Status.FILLED)
                else:
                    # 撤单先发生：不能有任何成交，买方市价单全额退款
                    self.assertEqual(maker.status, Order.Status.CANCELED)
                    self.assertEqual(outcomes["cancel"], "canceled")
                    self.assertEqual(trades, 0)
                    self.assertEqual(outcomes["buy_filled"], D("0"))
                    bob_usdt = Account.objects.get(user=self.bob, asset=self.usdt)
                    self.assertEqual(bob_usdt.available, D("100000000"))
                    self.assertEqual(bob_usdt.frozen, D("0"))

                result = reconcile_snapshot()
                self.assertTrue(result["ok"], result["mismatches"])


@unittest.skipUnless(MYSQL, "并发测试需要 MySQL（行锁 + 多连接）")
class ConcurrentSubmissionsTests(TransactionTestCase):

    def setUp(self):
        engine.reset_for_tests()
        self.usdt, _ = Asset.objects.get_or_create(symbol="USDT")
        self.btc, _ = Asset.objects.get_or_create(symbol="BTC")
        self.pair, _ = TradingPair.objects.get_or_create(
            symbol="BTCUSDT",
            defaults={
                "base": self.btc, "quote": self.usdt,
                "tick_size": D("0.01"), "lot_size": D("0.000001"),
                "min_notional": D("0"),
            },
        )
        self.maker = User.objects.create_user("maker_u", password="x")
        self.buyers = []
        from django.db import transaction as db_tx

        with db_tx.atomic():
            issue_simulated_asset(self.maker, self.btc, D("100000"))
            issue_simulated_asset(self.maker, self.usdt, D("100000000"))
            for i in range(10):
                u = User.objects.create_user(f"buyer_{i}", password="x")
                issue_simulated_asset(u, self.btc, D("1"))
                issue_simulated_asset(u, self.usdt, D("100000000"))
                self.buyers.append(u)
        SystemConfig.load()
        bootstrap_order_book()

    def test_ten_concurrent_market_buys_against_one_maker(self):
        """10 个线程各买 10 BTC，maker 挂 100 BTC：总量精确，守恒无超卖。"""
        maker_order, _ = engine.submit_order(
            user=self.maker, pair=self.pair, side="SELL", order_type="LIMIT",
            quantity=D("100"), limit_price=D("50000"), idem_key="big-ask")
        barrier = threading.Barrier(10)

        def buyer(idx):
            barrier.wait()
            o, _ = engine.submit_order(
                user=self.buyers[idx], pair=self.pair, side="BUY",
                order_type="MARKET", quantity=D("500000"),  # 预算 10 BTC
                idem_key=f"buy-{idx}")
            o.refresh_from_db()

        threads, boxes = zip(*[_thread(buyer, i) for i in range(10)])
        for t in threads:
            t.join(timeout=60)
        for i, box in enumerate(boxes):
            self.assertIsNone(box.get("error"), f"buyer {i}: {box.get('tb')}")

        # 直接从 DB 统计每个买方成交量
        total_bought = D("0")
        for u in self.buyers:
            bought = sum(
                Trade.objects.filter(taker_order__user=u)
                .values_list("quantity", flat=True),
                D("0"),
            )
            self.assertLessEqual(bought, D("10"))
            total_bought += bought

        maker_order.refresh_from_db()
        self.assertEqual(total_bought, D("100"))
        self.assertEqual(maker_order.filled_qty, D("100"))
        self.assertEqual(maker_order.status, Order.Status.FILLED)

        result = reconcile_snapshot()
        self.assertTrue(result["ok"], result["mismatches"])

        # maker BTC 冻结清零，USDT 全额到账（扣费后）
        maker_btc = Account.objects.get(user=self.maker, asset=self.btc)
        self.assertEqual(maker_btc.frozen, D("0"))
        self.assertEqual(maker_btc.available, D("99900"))  # 100000-100

    def test_concurrent_resting_orders_preserve_fifo(self):
        """同价并发挂单：进入簿后严格按 id 顺序（id 即提交先后）。"""
        barrier = threading.Barrier(6)
        users = self.buyers[:6]

        def seller(idx):
            barrier.wait()
            engine.submit_order(
                user=users[idx], pair=self.pair, side="SELL",
                order_type="LIMIT", quantity=D("1"),
                limit_price=D("50000"), idem_key=f"rest-{idx}")

        threads, boxes = zip(*[_thread(seller, i) for i in range(6)])
        for t in threads:
            t.join(timeout=30)
        for i, box in enumerate(boxes):
            self.assertIsNone(box.get("error"), f"seller {i}: {box.get('tb')}")

        snap = engine.book(self.pair.pk).snapshot()
        ask = next(a for a in snap["asks"] if a["price"] == "50000.00000000")
        ids = ask["order_ids"]
        self.assertEqual(ids, sorted(ids))  # FIFO = id 升序
        self.assertEqual(len(ids), 6)
