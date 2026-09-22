"""Concurrent submission to the same pair.

The cross-thread test only exercises real row locking on MySQL/InnoDB and is
skipped on sqlite.  It asserts that N concurrent orders against one thin
book leave: no negative balance, no orphan freeze, full ledger conservation,
and exactly one surviving trade per available quantity.
"""
from decimal import Decimal

from apps.accounts.models import Balance
from apps.ledger.management.commands.reconcile import Command as ReconcileCmd
from apps.trading import engine
from apps.trading.models import Order, Trade
from io import StringIO
from tests.base import EngineTestCase
from tests.factories import credit, make_market, make_user


class ConcurrentSubmissionTests(EngineTestCase):
    def test_many_buyers_race_thin_supply(self):
        if not self.is_mysql:
            self.skipTest("real concurrency requires MySQL/InnoDB row locks")

        import threading
        from django.core.management import call_command
        from django.db import connections
        from apps.trading.retry import retry_on_lock

        market = make_market()
        seller = make_user("seller")
        credit(seller, market.base_asset, "100")

        # Create and fund all buyers up front (avoids setup contention).
        racers = [make_user(f"racer{i}") for i in range(20)]
        for u in racers:
            credit(u, market.quote_asset, "1000000")

        # 5 BTC on sale.
        r = engine.submit_order(
            user=seller, market=market, type="LIMIT", side="SELL",
            price=Decimal("50000"), quantity=Decimal("5"),
        )
        engine.sync_book_after_commit(market.id, order=r.order)

        errors = []

        def buyer(user):
            try:
                retry_on_lock(lambda: engine.submit_order(
                    user=user, market=market, type="LIMIT", side="BUY",
                    price=Decimal("50000"), quantity=Decimal("1"),
                ), attempts=20)
            except Exception as exc:  # noqa: BLE001
                errors.append(repr(exc))
            finally:
                connections.close_all()

        threads = [threading.Thread(target=buyer, args=(u,)) for u in racers]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=60)

        self.assertEqual(errors, [])

        # Exactly 5 BTC traded: sum trade quantity == 5, seller fully filled.
        total_qty = sum(
            Decimal(t.quantity) for t in Trade.objects.filter(market=market)
        )
        self.assertEqual(total_qty, Decimal("5.00000000"))
        seller_order = Order.objects.get(user=seller)
        self.assertEqual(seller_order.status, Order.Status.FILLED)

        # No negative balances among real accounts (genesis equity excluded).
        for b in Balance.objects.exclude(is_equity=True):
            self.assertGreaterEqual(Decimal(b.available), 0)
            self.assertGreaterEqual(Decimal(b.frozen), 0)
        # Some buyer orders remain resting (price == ask); their freeze must
        # reconcile exactly against balances.
        out, err = StringIO(), StringIO()
        try:
            call_command("reconcile", "--quiet", stdout=out, stderr=err)
        except SystemExit as exc:
            if int(exc.code or 0) != 0:
                self.fail(out.getvalue() + err.getvalue())

    def test_duplicate_idempotency_key_under_threads(self):
        if not self.is_mysql:
            self.skipTest("real concurrency requires MySQL/InnoDB row locks")

        import threading
        from django.db import connections
        from apps.trading.retry import retry_on_lock

        market = make_market()
        alice = make_user("alice")
        credit(alice, market.base_asset, "10")
        payload = {
            "symbol": market.symbol, "type": "LIMIT", "side": "SELL",
            "price": "50000", "quantity": "1", "quote_amount": None,
        }
        results = []
        lock = threading.Lock()

        def submit():
            try:
                report = retry_on_lock(lambda: engine.submit_order(
                    user=alice, market=market, type="LIMIT", side="SELL",
                    price=Decimal("50000"), quantity=Decimal("1"),
                    idempotency_key="race-key", request_payload=payload,
                ), attempts=20)
                with lock:
                    results.append(report.order.id)
            except Exception as exc:  # noqa: BLE001
                with lock:
                    results.append(("error", repr(exc)))
            finally:
                connections.close_all()

        threads = [threading.Thread(target=submit) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=60)

        self.assertTrue(results)
        for r in results:
            self.assertNotIsInstance(r, tuple)
        # Every call returned the same order, and only one was ever created.
        self.assertEqual(set(results), {results[0]})
        self.assertEqual(Order.objects.filter(user=alice).count(), 1)
