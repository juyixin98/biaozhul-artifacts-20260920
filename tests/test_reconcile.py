"""Reconciliation: ledger conservation, balances replay, freeze integrity."""
from decimal import Decimal
from io import StringIO

from django.core.management import call_command
from django.core.management.base import SystemCheckError  # noqa: F401 (type hint)

from apps.trading import engine
from tests.base import EngineTestCase
from tests.factories import credit, make_market, make_user


def run_reconcile():
    out, err = StringIO(), StringIO()
    code = 0
    try:
        call_command("reconcile", stdout=out, stderr=err)
    except SystemExit as exc:
        code = int(exc.code or 0)
    return code, out.getvalue() + err.getvalue()


class ReconcileTests(EngineTestCase):
    def test_clean_book_passes(self):
        alice = make_user("alice")
        bob = make_user("bob")
        market = make_market()
        credit(alice, market.base_asset, "5")
        credit(bob, market.quote_asset, "500000")

        # A few trades, a partial cancel...
        r = engine.submit_order(
            user=alice, market=market, type="LIMIT", side="SELL",
            price=Decimal("50000"), quantity=Decimal("2"),
        )
        engine.sync_book_after_commit(market.id, order=r.order)

        fill = engine.submit_order(
            user=bob, market=market, type="LIMIT", side="BUY",
            price=Decimal("50000"), quantity=Decimal("0.7"),
        )
        engine.apply_book_changes(market.id, fill)

        engine.cancel_order(user=alice, order_id=r.order.id)

        # A market buy too
        r2 = engine.submit_order(
            user=alice, market=market, type="LIMIT", side="SELL",
            price=Decimal("49000"), quantity=Decimal("1"),
        )
        engine.sync_book_after_commit(market.id, order=r2.order)
        mb = engine.submit_order(
            user=bob, market=market, type="MARKET", side="BUY",
            quote_amount=Decimal("49000"),
        )
        engine.apply_book_changes(market.id, mb)

        code, output = run_reconcile()
        self.assertEqual(code, 0, output)
        self.assertIn("RECONCILIATION OK", output)

    def test_freeze_integrity_detects_working_order_freeze(self):
        # A resting order means balances.frozen == order.frozen_remaining;
        # reconcile accepts this as consistent.
        alice = make_user("alice")
        market = make_market()
        credit(alice, market.base_asset, "5")
        r = engine.submit_order(
            user=alice, market=market, type="LIMIT", side="SELL",
            price=Decimal("50000"), quantity=Decimal("2"),
        )
        engine.sync_book_after_commit(market.id, order=r.order)
        code, output = run_reconcile()
        self.assertEqual(code, 0, output)
