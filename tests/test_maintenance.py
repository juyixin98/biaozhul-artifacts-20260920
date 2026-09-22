"""Maintenance mode: stop accepting, cancel resting orders, release funds."""
from decimal import Decimal

from apps.accounts.models import Balance
from apps.trading import engine
from apps.trading.book_registry import registry
from apps.trading.models import Order
from tests.base import EngineTestCase
from tests.factories import balances, credit, make_admin, make_market, make_user


class MaintenanceTests(EngineTestCase):
    def setUp(self):
        super().setUp()
        self.admin = make_admin()
        self.alice = make_user("alice")
        self.bob = make_user("bob")
        self.market = make_market()
        credit(self.alice, self.market.base_asset, "10")
        credit(self.bob, self.market.quote_asset, "1000000")

    def test_maintenance_cancels_all_and_releases(self):
        orders = []
        for i in range(3):
            r = engine.submit_order(
                user=self.alice, market=self.market, type="LIMIT",
                side="SELL", price=Decimal(50000 + i),
                quantity=Decimal("1"),
            )
            engine.sync_book_after_commit(self.market.id, order=r.order)
            orders.append(r.order)
        r = engine.submit_order(
            user=self.bob, market=self.market, type="LIMIT",
            side="BUY", price=Decimal("40000"), quantity=Decimal("1"),
        )
        engine.sync_book_after_commit(self.market.id, order=r.order)
        orders.append(r.order)

        market, canceled_ids = engine.set_maintenance(
            admin_user=self.admin, market_id=self.market.id, enabled=True
        )
        registry.invalidate(self.market.id)

        self.assertTrue(market.in_maintenance)
        self.assertEqual(len(canceled_ids), 4)
        self.assertFalse(
            Order.objects.filter(
                id__in=[o.id for o in orders]
            ).exclude(status=Order.Status.CANCELED).exists()
        )
        # Every freeze released.
        self.assertFalse(Balance.objects.exclude(frozen=0).exists())
        # Recovered book is empty.
        book = registry.get(self.market.id)
        self.assertEqual(len(book.bids), 0)
        self.assertEqual(len(book.asks), 0)

    def test_maintenance_is_idempotent(self):
        r = engine.submit_order(
            user=self.alice, market=self.market, type="LIMIT",
            side="SELL", price=Decimal("50000"), quantity=Decimal("1"),
        )
        engine.sync_book_after_commit(self.market.id, order=r.order)

        _, ids1 = engine.set_maintenance(
            admin_user=self.admin, market_id=self.market.id, enabled=True
        )
        _, ids2 = engine.set_maintenance(
            admin_user=self.admin, market_id=self.market.id, enabled=True
        )
        self.assertEqual(ids1, [r.order.id])
        self.assertEqual(ids2, [])  # nothing left to cancel
        # Balances still consistent
        self.assertEqual(
            balances(self.alice)["BTC"],
            (Decimal("10.00000000"), Decimal("0")),
        )

    def test_orders_rejected_while_in_maintenance(self):
        engine.set_maintenance(
            admin_user=self.admin, market_id=self.market.id, enabled=True
        )
        with self.assertRaises(engine.OrderRejected):
            engine.submit_order(
                user=self.alice, market=self.market, type="LIMIT",
                side="SELL", price=Decimal("50000"), quantity=Decimal("1"),
            )

    def test_trading_resumes_after_maintenance_off(self):
        engine.set_maintenance(
            admin_user=self.admin, market_id=self.market.id, enabled=True
        )
        engine.set_maintenance(
            admin_user=self.admin, market_id=self.market.id, enabled=False
        )
        r = engine.submit_order(
            user=self.alice, market=self.market, type="LIMIT",
            side="SELL", price=Decimal("50000"), quantity=Decimal("1"),
        )
        engine.sync_book_after_commit(self.market.id, order=r.order)
        self.assertEqual(r.order.status, Order.Status.NEW)
