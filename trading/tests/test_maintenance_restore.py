"""
维护模式与重启恢复测试。
"""
from decimal import Decimal as D

from accounts.models import Account
from accounts.reconciliation import reconcile_snapshot
from trading.engine import bootstrap_order_book, engine
from trading.exceptions import MaintenanceActive
from trading.models import Order

from .base import BaseEngineTestCase


class MaintenanceTests(BaseEngineTestCase):

    def _seed_resting(self):
        orders = []
        for i, u in enumerate((self.alice, self.bob, self.carol)):
            o, _ = engine.submit_order(
                user=u, pair=self.pair, side="BUY", order_type="LIMIT",
                quantity=D("1"), limit_price=D("40000") + i, idem_key=f"r{i}")
            orders.append(o)
        return orders

    def test_enter_maintenance_drains_and_releases(self):
        orders = self._seed_resting()
        # 维护前冻结存在
        for o in orders:
            acc = Account.objects.get(pk=o.account_id)
            self.assertGreater(acc.frozen, D("0"))

        canceled = engine.enter_maintenance(actor=self.alice)
        self.assertEqual(canceled, 3)

        for o in orders:
            o.refresh_from_db()
            self.assertEqual(o.status, Order.Status.CANCELED)
            self.assertEqual(o.remaining_frozen, D("0"))
            acc = Account.objects.get(pk=o.account_id)
            self.assertEqual(acc.frozen, D("0"))

        # 簿已清空
        snap = engine.book(self.pair.pk).snapshot()
        self.assertEqual(snap["bids"], [])

        # 停止接单
        from django.contrib.auth.models import User

        with self.assertRaises(MaintenanceActive):
            engine.submit_order(
                user=self.bob, pair=self.pair, side="BUY", order_type="LIMIT",
                quantity=D("1"), limit_price=D("40000"), idem_key="rejected")

    def test_enter_maintenance_is_idempotent(self):
        self._seed_resting()
        n1 = engine.enter_maintenance(actor=self.alice)
        n2 = engine.enter_maintenance(actor=self.alice)  # 重复操作安全
        self.assertEqual((n1, n2), (3, 0))
        result = reconcile_snapshot()
        self.assertTrue(result["ok"], result["mismatches"])

    def test_exit_maintenance_resumes_trading(self):
        engine.enter_maintenance(actor=self.alice)
        engine.exit_maintenance(actor=self.alice)
        o, created = engine.submit_order(
            user=self.bob, pair=self.pair, side="BUY", order_type="LIMIT",
            quantity=D("1"), limit_price=D("40000"), idem_key="resume")
        self.assertTrue(created)
        self.assertEqual(o.status, Order.Status.NEW)


class RestoreTests(BaseEngineTestCase):

    def test_book_restores_price_time_order(self):
        # 同价三笔（乱序用户）与两档价格
        specs = [
            (self.carol, "SELL", D("51000"), 1),
            (self.alice, "SELL", D("50000"), 2),
            (self.bob, "SELL", D("50000"), 3),
            (self.alice, "BUY", D("49000"), 4),
        ]
        for u, side, price, i in specs:
            engine.submit_order(
                user=u, pair=self.pair, side=side, order_type="LIMIT",
                quantity=D("1"), limit_price=price, idem_key=f"o{i}")

        # 模拟进程重启：丢弃内存簿，从数据库重建
        engine.reset_for_tests()
        restored = bootstrap_order_book()
        self.assertEqual(restored["BTCUSDT"], 4)

        snap = engine.book(self.pair.pk).snapshot()
        # 卖盘：50000 档（alice,bob 按 id 升序）在前，51000 在后
        asks = snap["asks"]
        self.assertEqual(asks[0]["price"], "50000.00000000")
        self.assertEqual(asks[1]["price"], "51000.00000000")
        ask_50k_ids = asks[0]["order_ids"]
        self.assertEqual(ask_50k_ids, sorted(ask_50k_ids))
        # 买盘
        self.assertEqual(snap["bids"][0]["price"], "49000.00000000")

    def test_restored_book_matches_correctly(self):
        """重建后撮合结果与未重启一致，时间优先被保留。"""
        o_a, _ = engine.submit_order(
            user=self.alice, pair=self.pair, side="SELL", order_type="LIMIT",
            quantity=D("1"), limit_price=D("50000"), idem_key="a")
        o_b, _ = engine.submit_order(
            user=self.bob, pair=self.pair, side="SELL", order_type="LIMIT",
            quantity=D("1"), limit_price=D("50000"), idem_key="b")

        engine.reset_for_tests()
        bootstrap_order_book()

        m, _ = engine.submit_order(
            user=self.carol, pair=self.pair, side="BUY", order_type="MARKET",
            quantity=D("50000"), idem_key="m")
        from trading.models import Trade

        self.assertEqual(
            Trade.objects.filter(taker_order=m).order_by("id").first().maker_order_id,
            o_a.pk)

    def test_no_double_settlement_after_restart(self):
        """已成交订单不会在恢复后重新进入簿或被重复结算。"""
        engine.submit_order(
            user=self.alice, pair=self.pair, side="SELL", order_type="LIMIT",
            quantity=D("1"), limit_price=D("50000"), idem_key="a")
        m, _ = engine.submit_order(
            user=self.bob, pair=self.pair, side="BUY", order_type="MARKET",
            quantity=D("50000"), idem_key="m")
        # 市价买预算恰好买完 1 BTC：有成交即 FILLED，剩余取消语义见预算未耗尽场景
        self.assertEqual(m.status, Order.Status.FILLED)
        self.assertEqual(m.filled_qty, D("1"))
        self.assertEqual(m.remaining_frozen, D("0"))
        from trading.models import Trade

        before_trades = Trade.objects.count()

        engine.reset_for_tests()
        bootstrap_order_book()
        self.assertEqual(engine.book(self.pair.pk).resting, set())

        after_trades = Trade.objects.count()
        self.assertEqual(before_trades, after_trades)
        result = reconcile_snapshot()
        self.assertTrue(result["ok"], result["mismatches"])
