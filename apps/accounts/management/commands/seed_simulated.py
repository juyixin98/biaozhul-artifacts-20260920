"""Seed simulated data: assets, markets, system users, demo balances.

Everything here is *simulated* -- no blockchain, no real funds.  The command
is idempotent: running it repeatedly never mints duplicate balances, it only
tops demo balances back up to the requested amount.

Every simulated deposit is recorded in the double-entry ledger against the
system ``sim-bank`` user, so reconciliation always balances.
"""
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand
from django.db import transaction

from apps.accounts.models import Asset, Balance
from apps.accounts.services import (
    get_or_create_balance,
    get_or_create_system_user,
)
from apps.audit.services import record
from apps.ledger.models import LedgerEntry, LedgerEvent
from apps.ledger.services import mint_entries, post_event
from apps.markets.models import Market

User = get_user_model()

DEFAULT_ASSETS = [
    ("BTC", "Simulated Bitcoin", 1_000_000),
    ("ETH", "Simulated Ethereum", 10_000_000),
    ("USDT", "Simulated Tether", 1_000_000_000),
]

DEFAULT_MARKETS = [
    # base, quote, maker_bps, taker_bps
    ("BTC", "USDT", "10", "20"),
    ("ETH", "USDT", "10", "20"),
]


class Command(BaseCommand):
    help = "Initialise simulated assets, markets, fee account and demo users."

    def add_arguments(self, parser):
        parser.add_argument(
            "--demo-users", type=int, default=3,
            help="number of demo traders to create (alice/bob/carol...)",
        )
        parser.add_argument(
            "--demo-balance-usdt", default="1000000",
            help="USDT each demo user starts with",
        )
        parser.add_argument(
            "--demo-balance-btc", default="10",
            help="BTC each demo user starts with",
        )
        parser.add_argument(
            "--demo-balance-eth", default="100",
            help="ETH each demo user starts with",
        )
        parser.add_argument(
            "--admin-password", default="admin12345",
            help="password for the default admin user",
        )

    @transaction.atomic
    def handle(self, *args, **opts):
        from django.conf import settings

        bank, _ = get_or_create_system_user(settings.SIM_BANK_USERNAME)
        genesis, _ = get_or_create_system_user(settings.SIM_GENESIS_USERNAME)
        fee_user, _ = get_or_create_system_user(settings.FEE_ACCOUNT_USERNAME)
        self.stdout.write(
            f"system users: {genesis.username}, {bank.username}, "
            f"{fee_user.username}"
        )

        # --- assets + simulated mint genesis -> sim-bank -------------------
        assets = {}
        for code, name, mint in DEFAULT_ASSETS:
            asset, created = Asset.objects.get_or_create(
                code=code, defaults={"name": name}
            )
            assets[code] = asset
            bank_bal = get_or_create_balance(bank, asset)
            genesis_bal = get_or_create_balance(
                genesis, asset, is_equity=True
            )
            already_minted = LedgerEntry.objects.filter(
                user=genesis, asset=asset
            ).exists()
            if created or not already_minted:
                mint = Decimal(mint)
                bank_bal.available += mint
                genesis_bal.available -= mint
                Balance.objects.bulk_update(
                    [bank_bal, genesis_bal],
                    ["available", "frozen", "updated_at"],
                )
                post_event(
                    LedgerEvent.EventType.DEPOSIT,
                    mint_entries(
                        bank, genesis, asset, mint, bank_bal, genesis_bal
                    ),
                    note=f"simulated mint {mint} {code}",
                )
                self.stdout.write(f"minted {mint} {code} to {bank.username}")

        # --- markets --------------------------------------------------------
        for base_code, quote_code, maker_bps, taker_bps in DEFAULT_MARKETS:
            symbol = f"{base_code}-{quote_code}"
            market, created = Market.objects.get_or_create(
                symbol=symbol,
                defaults={
                    "base_asset": assets[base_code],
                    "quote_asset": assets[quote_code],
                    "maker_fee_bps": Decimal(maker_bps),
                    "taker_fee_bps": Decimal(taker_bps),
                    "min_quantity": Decimal("0.0001"),
                    "min_notional": Decimal("1"),
                },
            )
            self.stdout.write(
                f"market {symbol} {'created' if created else 'already exists'}"
            )

        # --- admin ----------------------------------------------------------
        admin, created = User.objects.get_or_create(
            username="admin",
            defaults={"is_staff": True, "is_superuser": True},
        )
        if created:
            admin.set_password(opts["admin_password"])
            admin.save()
            self.stdout.write(
                f"admin user created (username=admin password="
                f"{opts['admin_password']})"
            )

        # --- demo users with simulated deposits ----------------------------
        demo_names = ["alice", "bob", "carol", "dave", "erin", "frank",
                      "grace", "heidi"]
        amounts = {
            "USDT": Decimal(opts["demo_balance_usdt"]),
            "BTC": Decimal(opts["demo_balance_btc"]),
            "ETH": Decimal(opts["demo_balance_eth"]),
        }
        for i in range(opts["demo_users"]):
            username = demo_names[i] if i < len(demo_names) else f"trader{i+1}"
            user, created = User.objects.get_or_create(username=username)
            if created:
                user.set_password("demo12345")
                user.save()
            for code, amount in amounts.items():
                self._top_up(user, assets[code], amount, bank)
            self.stdout.write(f"demo user {username} ready (password demo12345)")

        self.stdout.write(self.style.SUCCESS("seed complete"))

    def _top_up(self, user, asset, target, bank):
        """Bring a demo balance up to ``target`` via a sim-bank transfer."""
        bal = get_or_create_balance(user, asset)
        if bal.available >= target:
            return
        delta = target - bal.available
        bank_bal = Balance.objects.select_for_update().get(
            user=bank, asset=asset
        )
        if bank_bal.available < delta:
            raise RuntimeError(
                f"sim-bank insufficient for {delta} {asset.code}"
            )
        bank_bal.available -= delta
        user_bal = Balance.objects.select_for_update().get(
            user=user, asset=asset
        )
        user_bal.available += delta
        Balance.objects.bulk_update(
            [bank_bal, user_bal], ["available", "frozen", "updated_at"]
        )
        post_event(
            LedgerEvent.EventType.DEPOSIT,
            [
                {
                    "user": bank, "asset": asset,
                    "subaccount": LedgerEntry.SubAccount.AVAILABLE,
                    "amount": -delta, "balance_after": bank_bal.available,
                },
                {
                    "user": user, "asset": asset,
                    "subaccount": LedgerEntry.SubAccount.AVAILABLE,
                    "amount": delta, "balance_after": user_bal.available,
                },
            ],
            note=f"simulated deposit to {user.username}",
        )
        record(
            "DEPOSIT", actor=user, target=asset.code,
            detail={"amount": str(delta), "simulated": True},
        )
