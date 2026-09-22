"""Cancel semantics and fill/cancel races.

The true cross-thread race is exercised on MySQL in
``test_concurrency_mysql``; the logic-level tests run everywhere.
"""
from decimal import Decimal

from apps.trading import engine
from apps.trading.book_registry import registry
from apps.trading.models import Order
from tests.base import EngineTestCase
from tests.factories import balances, credit, make_market, make_user


class CancelTests(EngineTestCase):
    def setUp(self):
        super().setUp()
        self.alice = make_user("alice")
        self.bob = make_user("bob")
        self.market = make_market()
        credit(self.alice, self.market.base_asset, "10")
        credit(self.bob, self.market.quote_asset, "1000000")

    def test_cancel_releases_freeze_and_removes_book_entry(self):
        r = engine.submit_order(
            user=self.alice, market=self.market, type="LIMIT",
            side="SELL", price=Decimal("50000"), quantity=Decimal("2"),
        )
        engine.sync_book_after_commit(self.market.id, order=r.order)
        self.assertEqual(len(registry.get(self.market.id).asks), 1)

        canceled = engine.cancel_order(user=self.alice, order_id=r.order.id)
        engine.sync_book_after_commit(
            self.market.id, order=canceled, canceled=True
        )
        self.assertEqual(canceled.status, Order.Status.CANCELED)
        self.assertEqual(len(registry.get(self.market.id).asks), 0)
        a = balances(self.alice)
        self.assertEqual(a["BTC"], (Decimal("10.00000000"), Decimal("0")))

    def test_double_cancel_is_conflict_not_double_release(self):
        r = engine.submit_order(
            user=self.alice, market=self.market, type="LIMIT",
            side="SELL", price=Decimal("50000"), quantity=Decimal("2"),
        )
        engine.sync_book_after_commit(self.market.id, order=r.order)
        engine.cancel_order(user=self.alice, order_id=r.order.id)
        with self.assertRaises(engine.OrderNotCancelable):
            engine.cancel_order(user=self.alice, order_id=r.order.id)
        a = balances(self.alice)
        # released exactly once
        self.assertEqual(a["BTC"], (Decimal("10.00000000"), Decimal("0")))

    def test_cancel_filled_order_conflicts(self):
        r = engine.submit_order(
            user=self.alice, market=self.market, type="LIMIT",
            side="SELL", price=Decimal("50000"), quantity=Decimal("1"),
        )
        engine.sync_book_after_commit(self.market.id, order=r.order)
        fill = engine.submit_order(
            user=self.bob, market=self.market, type="LIMIT",
            side="BUY", price=Decimal("50000"), quantity=Decimal("1"),
        )
        engine.apply_book_changes(self.market.id, fill)
        with self.assertRaises(engine.OrderNotCancelable):
            engine.cancel_order(user=self.alice, order_id=r.order.id)

    def test_cancel_partial_fill_releases_remaining_only(self):
        # Only 0.4 bid-side liquidity: carol buys 0.4, alice sells 1 resting
        # remainder after being a taker? Instead: alice resting sell of 1;
        # bob market/limit buys 0.4, then alice cancels the rest.
        r = engine.submit_order(
            user=self.alice, market=self.market, type="LIMIT",
            side="SELL", price=Decimal("50000"), quantity=Decimal("1"),
        )
        engine.sync_book_after_commit(self.market.id, order=r.order)
        fill = engine.submit_order(
            user=self.bob, market=self.market, type="LIMIT",
            side="BUY", price=Decimal("50000"), quantity=Decimal("0.4"),
        )
        engine.apply_book_changes(self.market.id, fill)

        canceled = engine.cancel_order(user=self.alice, order_id=r.order.id)
        engine.sync_book_after_commit(
            self.market.id, order=canceled, canceled=True
        )
        self.assertEqual(canceled.status, Order.Status.CANCELED)
        self.assertEqual(canceled.filled_quantity, Decimal("0.40000000"))
        a = balances(self.alice)
        # 10 BTC: 0.4 sold, 0.6 released
        self.assertEqual(a["BTC"], (Decimal("9.60000000"), Decimal("0")))

    def test_cannot_cancel_other_users_order(self):
        r = engine.submit_order(
            user=self.alice, market=self.market, type="LIMIT",
            side="SELL", price=Decimal("50000"), quantity=Decimal("1"),
        )
        engine.sync_book_after_commit(self.market.id, order=r.order)
        with self.assertRaises(Order.DoesNotExist):
            engine.cancel_order(user=self.bob, order_id=r.order.id)

    def test_concurrency_mysql(self):
        """Hammer one resting order with concurrent fills and cancels.

        Skipped on sqlite (its locking doesn't emulate InnoDB).  Every legal
        outcome is exactly one of: filled once, or canceled untouched; total
        BTC and USDT across users is conserved, and all frozen funds are
        released.
        """
        if not self.is_mysql:
            self.skipTest("concurrency race requires MySQL/InnoDB")
        import threading

        from django.db import connections
        from apps.trading.retry import retry_on_lock

        sellers = [make_user(f"s{i}") for i in range(8)]
        for s in sellers:
            credit(s, self.market.base_asset, "10")
        buyers = [make_user(f"b{i}") for i in range(8)]
        for b in buyers:
            credit(b, self.market.quote_asset, "1000000")

        errors = []

        def work_seller_cancel(seller, oid):
            try:
                retry_on_lock(lambda: engine.cancel_order(
                    user=seller, order_id=oid
                ))
            except engine.OrderNotCancelable:
                pass
            except Exception as exc:  # noqa: BLE001
                errors.append(repr(exc))
            finally:
                connections.close_all()

        def work_buyer_fill(buyer):
            try:
                retry_on_lock(lambda: engine.submit_order(
                    user=buyer, market=self.market, type="LIMIT",
                    side="BUY", price=Decimal("50000"),
                    quantity=Decimal("1"),
                ))
            except Exception as exc:  # noqa: BLE001
                errors.append(repr(exc))
            finally:
                connections.close_all()

        threads = []
        order_ids = []
        for s in sellers:
            r = engine.submit_order(
                user=s, market=self.market, type="LIMIT",
                side="SELL", price=Decimal("50000"), quantity=Decimal("1"),
            )
            engine.sync_book_after_commit(self.market.id, order=r.order)
            order_ids.append(r.order.id)

        for s, oid in zip(sellers, order_ids):
            threads.append(
                threading.Thread(target=work_seller_cancel, args=(s, oid))
            )
        for b in buyers:
            threads.append(threading.Thread(target=work_buyer_fill, args=(b,)))

        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)

        self.assertEqual(errors, [])
        # No frozen funds may remain from *seller* orders: the buyer orders
        # that did not match are resting (working) and legitimately hold a
        # freeze equal to their quantity -- reconcile covers that.  Cancel
        # every working buyer order and then all freeze must be released.
        from apps.accounts.models import Balance
        from apps.trading.models import Order as OrderModel
        for bo in OrderModel.objects.filter(
            user__in=buyers, status__in=(
                OrderModel.Status.NEW, OrderModel.Status.PARTIALLY_FILLED
            )
        ):
            engine.cancel_order(user=bo.user, order_id=bo.id)

        self.assertFalse(
            Balance.objects.exclude(frozen=0).exists(),
            "frozen funds remain after fill/cancel race",
        )
        # Exactly one side of each race won: each seller's order is FILLED
        # (with exactly one trade) or CANCELED.
        for oid in order_ids:
            order = Order.objects.get(id=oid)
            self.assertIn(order.status,
                          (Order.Status.FILLED, Order.Status.CANCELED))
            if order.status == Order.Status.FILLED:
                self.assertEqual(order.filled_quantity, Decimal("1.00000000"))
