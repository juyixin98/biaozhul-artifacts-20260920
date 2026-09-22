"""In-memory book recovery from persisted state after a restart."""
from decimal import Decimal

from apps.trading import engine
from apps.trading.book_registry import registry
from apps.trading.models import Order
from apps.trading.orderbook import OrderBook
from tests.base import EngineTestCase
from tests.factories import credit, make_market, make_user


class RecoveryTests(EngineTestCase):
    def setUp(self):
        super().setUp()
        self.alice = make_user("alice")
        self.bob = make_user("bob")
        self.carol = make_user("carol")
        self.market = make_market()
        for u in (self.alice, self.carol):
            credit(u, self.market.base_asset, "100")
        credit(self.bob, self.market.quote_asset, "10000000")

    def _sell(self, user, price, qty):
        r = engine.submit_order(
            user=user, market=self.market, type="LIMIT",
            side="SELL", price=Decimal(price), quantity=Decimal(qty),
        )
        engine.sync_book_after_commit(self.market.id, order=r.order)
        return r.order

    def test_book_rebuild_preserves_price_time_order(self):
        # Build a book with levels and partial fills:
        o1 = self._sell(self.alice, "50100", "1")
        o2 = self._sell(self.carol, "50000", "2")   # best ask
        o3 = self._sell(self.alice, "50000", "1")   # same price, later
        o4 = self._sell(self.carol, "50200", "0.5")

        # Partially fill o2 via a 0.4 market/limit buy.
        r = engine.submit_order(
            user=self.bob, market=self.market, type="LIMIT",
            side="BUY", price=Decimal("50000"), quantity=Decimal("0.4"),
        )
        engine.apply_book_changes(self.market.id, r)

        # Simulate a full process restart: drop the cache and rebuild.
        registry.invalidate(self.market.id)
        recovered = OrderBook.rebuild(self.market.id)

        asks = list(recovered.asks.best_iter())
        # Expected ordering: 50000/o2(1.6), 50000/o3(1), 50100/o1(1),
        # 50200/o4(0.5)
        self.assertEqual(
            [(l.order_id, l.price, l.remaining) for l in asks],
            [
                (o2.id, Decimal("50000.00000000"), Decimal("1.60000000")),
                (o3.id, Decimal("50000.00000000"), Decimal("1.00000000")),
                (o1.id, Decimal("50100.00000000"), Decimal("1.00000000")),
                (o4.id, Decimal("50200.00000000"), Decimal("0.50000000")),
            ],
        )

    def test_recovered_book_matches_correctly(self):
        o1 = self._sell(self.alice, "50000", "1")
        registry.invalidate(self.market.id)  # restart

        report = engine.submit_order(
            user=self.bob, market=self.market, type="LIMIT",
            side="BUY", price=Decimal("50000"), quantity=Decimal("1"),
        )
        engine.apply_book_changes(self.market.id, report)
        self.assertEqual(len(report.trades), 1)
        self.assertEqual(report.trades[0].maker_order_id, o1.id)
        o1.refresh_from_db()
        self.assertEqual(o1.status, Order.Status.FILLED)

    def test_recovered_book_excludes_canceled_and_market_orders(self):
        o1 = self._sell(self.alice, "50000", "1")
        engine.cancel_order(user=self.alice, order_id=o1.id)
        registry.invalidate(self.market.id)
        recovered = OrderBook.rebuild(self.market.id)
        self.assertEqual(len(recovered.asks), 0)

        # A market buy that gets canceled (no liquidity) must not linger in
        # a recovered book even at its NEW mid-state.
        engine.submit_order(
            user=self.bob, market=self.market, type="MARKET",
            side="BUY", quote_amount=Decimal("100"),
        )
        registry.invalidate(self.market.id)
        recovered = OrderBook.rebuild(self.market.id)
        self.assertEqual(len(recovered.bids), 0)
