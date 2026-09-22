"""Reconciliation example / command.

Four checks:

1. **Ledger conservation** -- the signed sum of *all* ledger entries is zero
   per asset.  Every event is balanced: simulated mints are credited to the
   sim-bank and debited from a ``sim-genesis`` equity account, so they add
   nothing to the total either.
2. **Ledger vs balances** -- summing entries per (user, asset, subaccount)
   reproduces the current Balance rows exactly, including the fee account.
3. **Freeze integrity** -- the sum of ``frozen_remaining`` over working
   orders equals the owner's ``Balance.frozen`` for every user/asset.
4. **Non-negative balances** -- no account may be negative except the
   genesis equity account (whose negative balance *is* the simulated
   supply source).

Exit code is non-zero when any check fails, so the command can be wired
into monitoring / CI.
"""
from collections import defaultdict
from decimal import Decimal

from django.conf import settings
from django.core.management.base import BaseCommand

from apps.accounts.models import Balance
from apps.ledger.models import LedgerEntry
from apps.trading.models import Order
from apps.trading.orderbook import WORKING

ZERO = Decimal("0")


class Command(BaseCommand):
    help = "Reconcile ledger conservation, balances and order freezes."

    def add_arguments(self, parser):
        parser.add_argument(
            "--quiet", action="store_true", help="only print problems"
        )

    def handle(self, *args, **opts):
        quiet = opts["quiet"]
        problems = []

        # 1+2. replay every ledger entry ------------------------------------
        ledger_balance = defaultdict(lambda: ZERO)  # (uid, aid, sub) -> sum
        per_asset_total = defaultdict(lambda: ZERO)
        for entry in (
            LedgerEntry.objects.values(
                "user_id", "asset_id", "subaccount", "amount"
            ).iterator(chunk_size=2000)
        ):
            key = (entry["user_id"], entry["asset_id"], entry["subaccount"])
            amount = Decimal(entry["amount"])
            ledger_balance[key] += amount
            per_asset_total[entry["asset_id"]] += amount

        for asset_id, total in sorted(per_asset_total.items()):
            if total != ZERO:
                problems.append(
                    f"[conservation] asset {asset_id}: ledger entries sum "
                    f"to {total}, expected 0"
                )
            elif not quiet:
                self.stdout.write(f"[conservation] asset {asset_id}: OK (sum=0)")

        rows = 0
        for bal in Balance.objects.select_related("asset", "user").iterator():
            rows += 1
            for sub, actual in (
                ("AVAILABLE", Decimal(bal.available)),
                ("FROZEN", Decimal(bal.frozen)),
            ):
                expected = ledger_balance.get(
                    (bal.user_id, bal.asset_id, sub), ZERO
                )
                if expected != actual:
                    problems.append(
                        f"[balance] user={bal.user.username} "
                        f"asset={bal.asset.code} sub={sub}: "
                        f"ledger says {expected}, balance row says {actual}"
                    )
        if not quiet:
            self.stdout.write(f"[balance] compared {rows} balance rows")

        # 3. freeze integrity ------------------------------------------------
        order_freeze = defaultdict(lambda: ZERO)  # (uid, aid) -> frozen sum
        for order in (
            Order.objects.filter(status__in=WORKING)
            .select_related(
                "market", "market__base_asset", "market__quote_asset"
            )
        ):
            asset = (
                order.market.quote_asset if order.side == "BUY"
                else order.market.base_asset
            )
            order_freeze[(order.user_id, asset.id)] += Decimal(
                order.frozen_remaining
            )

        frozen_rows = {
            (b.user_id, b.asset_id): Decimal(b.frozen)
            for b in Balance.objects.exclude(frozen=0)
        }
        all_keys = set(order_freeze) | set(frozen_rows)
        for key in sorted(all_keys):
            of = order_freeze.get(key, ZERO)
            bf = frozen_rows.get(key, ZERO)
            if of != bf:
                problems.append(
                    f"[freeze] user={key[0]} asset={key[1]}: "
                    f"working orders freeze {of}, balance frozen {bf}"
                )
        if not quiet:
            self.stdout.write(
                f"[freeze] {len(all_keys)} user/asset buckets with frozen funds"
            )

        # 4. non-negative balances (genesis equity is the sole exception) ---
        for bal in (
            Balance.objects.exclude(
                user__username=settings.SIM_GENESIS_USERNAME
            )
            .select_related("asset", "user")
        ):
            if Decimal(bal.available) < ZERO or Decimal(bal.frozen) < ZERO:
                problems.append(
                    f"[negative] user={bal.user.username} "
                    f"asset={bal.asset.code} "
                    f"available={bal.available} frozen={bal.frozen}"
                )

        if problems:
            self.stderr.write(self.style.ERROR(
                f"RECONCILIATION FAILED: {len(problems)} problem(s)"
            ))
            for p in problems:
                self.stderr.write("  " + p)
            raise SystemExit(1)

        self.stdout.write(self.style.SUCCESS("RECONCILIATION OK"))
