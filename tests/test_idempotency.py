"""Idempotency keys: repeat returns same order; different body = conflict."""
from decimal import Decimal

from apps.trading import engine
from apps.trading.models import IdempotencyKey, Order
from tests.base import EngineTestCase
from tests.factories import credit, make_market, make_user


class IdempotencyTests(EngineTestCase):
    def setUp(self):
        super().setUp()
        self.alice = make_user("alice")
        self.market = make_market()
        credit(self.alice, self.market.base_asset, "10")

    def _payload(self, **over):
        p = {
            "symbol": "BTC-USDT", "type": "LIMIT", "side": "SELL",
            "price": "50000", "quantity": "1",
            "quote_amount": None,
        }
        p.update(over)
        return p

    def test_same_key_replays_order_without_new_freeze(self):
        payload = self._payload()
        r1 = engine.submit_order(
            user=self.alice, market=self.market, type="LIMIT",
            side="SELL", price=Decimal("50000"), quantity=Decimal("1"),
            idempotency_key="k-1", request_payload=payload,
        )
        r2 = engine.submit_order(
            user=self.alice, market=self.market, type="LIMIT",
            side="SELL", price=Decimal("50000"), quantity=Decimal("1"),
            idempotency_key="k-1", request_payload=payload,
        )
        self.assertTrue(r2.replayed)
        self.assertEqual(r1.order.id, r2.order.id)
        self.assertEqual(Order.objects.filter(user=self.alice).count(), 1)
        self.assertEqual(IdempotencyKey.objects.count(), 1)

    def test_same_key_different_params_conflicts(self):
        engine.submit_order(
            user=self.alice, market=self.market, type="LIMIT",
            side="SELL", price=Decimal("50000"), quantity=Decimal("1"),
            idempotency_key="k-2", request_payload=self._payload(),
        )
        with self.assertRaises(engine.Conflict):
            engine.submit_order(
                user=self.alice, market=self.market, type="LIMIT",
                side="SELL", price=Decimal("50001"), quantity=Decimal("1"),
                idempotency_key="k-2",
                request_payload=self._payload(price="50001"),
            )
        # original order untouched
        order = Order.objects.get(user=self.alice)
        self.assertEqual(order.price, Decimal("50000.00000000"))

    def test_keys_are_scoped_per_user(self):
        bob = make_user("bob")
        credit(bob, self.market.base_asset, "10")
        payload = self._payload()
        engine.submit_order(
            user=self.alice, market=self.market, type="LIMIT",
            side="SELL", price=Decimal("50000"), quantity=Decimal("1"),
            idempotency_key="shared", request_payload=payload,
        )
        # Same key string for a different user must NOT conflict.
        report = engine.submit_order(
            user=bob, market=self.market, type="LIMIT",
            side="SELL", price=Decimal("50000"), quantity=Decimal("1"),
            idempotency_key="shared", request_payload=payload,
        )
        self.assertFalse(report.replayed)
        self.assertEqual(Order.objects.count(), 2)
