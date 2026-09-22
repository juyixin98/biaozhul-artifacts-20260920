"""Targeted edge cases for frozen-remaining accounting across fills.

These guard the floor(cost) vs half-up(freeze) interaction for a limit BUY
that first rests as a partially-filled taker and is later hit repeatedly as
a maker -- the freeze must stay non-negative and reconcile to exactly
price * remaining while resting and 0 when terminal.
"""
from decimal import Decimal

from apps.trading import engine
from apps.trading.models import Order
from tests.base import EngineTestCase
from tests.factories import credit, make_market, make_user


class BuyFreezeAcrossFillsTests(EngineTestCase):
    def setUp(self):
        super().setUp()
        self.m = make_market(min_notional="0", min_quantity="0",
                             maker_bps="0", taker_bps="0")
        self.btc = self.m.base_asset
        self.usdt = self.m.quote_asset
        self.buyer = make_user("buyer")
        self.s1 = make_user("s1")
        self.s2 = make_user("s2")
        credit(self.buyer, self.usdt, "100000000")
        credit(self.s1, self.btc, "1000")
        credit(self.s2, self.btc, "1000")

    def test_buy_taker_partial_then_maker_filled(self):
        # 0.4 BTC offered at 50000 -> buyer bids 1, fills 0.4, rests 0.6
        r = engine.submit_order(
            user=self.s1, market=self.m, type="LIMIT", side="SELL",
            price=Decimal("50000"), quantity=Decimal("0.4"),
        )
        engine.sync_book_after_commit(self.m.id, order=r.order)

        rep = engine.submit_order(
            user=self.buyer, market=self.m, type="LIMIT", side="BUY",
            price=Decimal("50000"), quantity=Decimal("1"),
        )
        engine.apply_book_changes(self.m.id, rep)
        bid = rep.order
        bid.refresh_from_db()
        self.assertEqual(bid.status, Order.Status.PARTIALLY_FILLED)
        # freeze = half_up(50000) = 50000; consumed floor(50000*0.4)=20000
        self.assertEqual(bid.frozen_remaining, Decimal("30000.00000000"))

        # Now a seller hits the resting bid for 0.1 (buyer is MAKER).
        rep2 = engine.submit_order(
            user=self.s2, market=self.m, type="LIMIT", side="SELL",
            price=Decimal("50000"), quantity=Decimal("0.1"),
        )
        engine.apply_book_changes(self.m.id, rep2)
        bid.refresh_from_db()
        self.assertEqual(bid.status, Order.Status.PARTIALLY_FILLED)
        self.assertEqual(bid.filled_quantity, Decimal("0.50000000"))
        # maker buyer freeze consumed by cost floor(50000*0.1)=5000
        self.assertEqual(bid.frozen_remaining, Decimal("25000.00000000"))

        # Seller crosses the rest 0.5 -> buyer fully FILLED.
        rep3 = engine.submit_order(
            user=self.s2, market=self.m, type="LIMIT", side="SELL",
            price=Decimal("50000"), quantity=Decimal("0.5"),
        )
        engine.apply_book_changes(self.m.id, rep3)
        bid.refresh_from_db()
        self.assertEqual(bid.status, Order.Status.FILLED)
        self.assertEqual(bid.filled_quantity, Decimal("1.00000000"))
        self.assertEqual(bid.frozen_remaining, Decimal("0"))

    def test_buy_freeze_with_awkward_price_stays_nonnegative(self):
        # Price 3.3 with awkward per-fill floor dust across 3 fills.
        # Seller supplies 3 x 0.33333333 (~0.99999999); buyer bids 1.
        for i in range(3):
            r = engine.submit_order(
                user=self.s1, market=self.m, type="LIMIT", side="SELL",
                price=Decimal("3.3"), quantity=Decimal("0.33333333"),
            )
            engine.sync_book_after_commit(self.m.id, order=r.order)

        rep = engine.submit_order(
            user=self.buyer, market=self.m, type="LIMIT", side="BUY",
            price=Decimal("3.3"), quantity=Decimal("1"),
        )
        engine.apply_book_changes(self.m.id, rep)
        # It should fully fill (0.99999999 available, buyer wants 1):
        # buyer takes what exists and rests for the missing 0.00000001 or
        # is partially filled -- either way freeze must be non-negative and
        # exactly price*remaining.
        bid = rep.order
        bid.refresh_from_db()
        self.assertGreaterEqual(bid.frozen_remaining, Decimal("0"))
        if bid.is_working:
            expected = (Decimal("3.3") * bid.remaining_quantity)
            # floor/half-up may differ by < 1e-8; allow one dust unit
            self.assertLessEqual(
                abs(bid.frozen_remaining - expected), Decimal("0.00000001")
            )
