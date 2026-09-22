"""Rounding edge cases: 8-dp limits, floor costs, half-up fees."""
from decimal import Decimal

from apps.common.decimals import (
    fee_from_bps,
    limit_buy_freeze,
    multiply_price_qty,
    quantize_floor,
    quantize_half_up,
)
from apps.trading import engine
from apps.trading.models import Order
from tests.base import EngineTestCase
from tests.factories import balances, credit, make_market, make_user


class DecimalHelperTests(EngineTestCase):
    def test_fee_half_up(self):
        # 0.123456789 * 10 bps -> 0.000123456789 -> half up 0.00012346
        self.assertEqual(
            fee_from_bps(Decimal("0.123456789"), Decimal("10")),
            Decimal("0.00012346"),
        )
        # 0.0005 notional at 10 bps = 0.0005*10/10000 = 0.0000005
        self.assertEqual(
            fee_from_bps(Decimal("0.0005"), Decimal("10")),
            Decimal("0.00000050"),
        )
        # exact tie at 8.5 dp: fee = 0.001 * 0.05 bps / 10000 = 5e-9
        # -> half up 1e-8
        self.assertEqual(
            fee_from_bps(Decimal("0.001"), Decimal("0.05")),
            Decimal("0.00000001"),
        )
        # ...and a small exact fee: 0.0004 notional at 1 bps = 4e-8
        self.assertEqual(
            fee_from_bps(Decimal("0.0004"), Decimal("1")),
            Decimal("0.00000004"),
        )
        # below-half rounds down: 0.0004 notional at 0.1 bps = 4e-9 -> 0
        self.assertEqual(
            fee_from_bps(Decimal("0.0004"), Decimal("0.1")),
            Decimal("0.00000000"),
        )

    def test_cost_is_floored(self):
        # 0.00000003 * 0.3 = 0.000000009 -> floor 0.00000000
        self.assertEqual(
            multiply_price_qty(Decimal("0.00000003"), Decimal("0.3")),
            Decimal("0.00000000"),
        )
        # 1.234567895 -> floor 1.23456789
        self.assertEqual(
            multiply_price_qty(Decimal("1"), Decimal("1.234567895")),
            Decimal("1.23456789"),
        )

    def test_freeze_half_up_covers_floored_costs(self):
        freeze = limit_buy_freeze(Decimal("3"), Decimal("1.00000001"))
        # 3.00000003 half-up -> 3.00000003
        self.assertEqual(freeze, Decimal("3.00000003"))
        # Two fills of that quantity: each floor(3*1.00000000 + 0.00000003)
        # = floor(3.00000003) = 3.00000003; sum=6.00000006; freeze for
        # 2.00000002 units = half_up(6.00000006) -> 6.00000006. OK.
        total = quantize_half_up(Decimal("3") * Decimal("2.00000002"))
        per = multiply_price_qty(Decimal("3"), Decimal("1.00000001"))
        self.assertLessEqual(per * 2, total)


class RoundingFlowTests(EngineTestCase):
    def setUp(self):
        super().setUp()
        self.alice = make_user("alice")
        self.bob = make_user("bob")
        # min_notional 0 so tiny fills are accepted.
        self.market = make_market(min_notional="0", min_quantity="0")
        credit(self.alice, self.market.base_asset, "100")
        credit(self.bob, self.market.quote_asset, "10000000")

    def test_buyer_never_negative_with_dust_prices(self):
        # Ask 1 base at a price producing per-fill floor dust.
        r = engine.submit_order(
            user=self.alice, market=self.market, type="LIMIT",
            side="SELL", price=Decimal("0.00000003"),
            quantity=Decimal("1.00000000"),
        )
        engine.sync_book_after_commit(self.market.id, order=r.order)

        report = engine.submit_order(
            user=self.bob, market=self.market, type="LIMIT",
            side="BUY", price=Decimal("0.00000003"),
            quantity=Decimal("1.00000000"),
        )
        engine.apply_book_changes(self.market.id, report)
        order = report.order
        order.refresh_from_db()
        self.assertEqual(order.status, Order.Status.FILLED)
        # frozen_remaining can't be negative
        self.assertGreaterEqual(order.frozen_remaining, Decimal("0"))
        # Quote charged: floor(0.00000003 * 1) = 0.00000003
        b = balances(self.bob)
        self.assertEqual(
            b["USDT"],
            (Decimal("10000000") - Decimal("0.00000003"), Decimal("0")),
        )
        # fee on base received: taker 20bps * 1 = 0.002 BTC
        self.assertEqual(b["BTC"], (Decimal("0.99800000"), Decimal("0")))

    def test_fee_account_holds_collected_fees(self):
        r = engine.submit_order(
            user=self.alice, market=self.market, type="LIMIT",
            side="SELL", price=Decimal("1000"), quantity=Decimal("1"),
        )
        engine.sync_book_after_commit(self.market.id, order=r.order)
        report = engine.submit_order(
            user=self.bob, market=self.market, type="LIMIT",
            side="BUY", price=Decimal("1000"), quantity=Decimal("1"),
        )
        engine.apply_book_changes(self.market.id, report)

        from django.contrib.auth import get_user_model
        fee_user = get_user_model().objects.get(username="fee-account")
        fee_base = balances(fee_user)["BTC"]
        fee_quote = balances(fee_user)["USDT"]
        # taker (buyer) fee 0.002 BTC; maker (seller) fee 1 USDT
        self.assertEqual(fee_base, (Decimal("0.00200000"), Decimal("0")))
        self.assertEqual(fee_quote, (Decimal("1.00000000"), Decimal("0")))
