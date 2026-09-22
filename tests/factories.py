"""Test helpers: build users, assets, markets, balances quickly."""
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.db import transaction

from apps.accounts.models import Asset, Balance
from apps.accounts.services import get_or_create_system_user
from apps.ledger.models import LedgerEntry, LedgerEvent
from apps.ledger.services import post_event
from apps.markets.models import Market

User = get_user_model()


def make_user(username, password="x1234567", **kw):
    return User.objects.create_user(username=username, password=password, **kw)


def make_admin(username="admin", password="admin12345"):
    return User.objects.create_user(
        username=username, password=password, is_staff=True
    )


def make_asset(code, name=None):
    return Asset.objects.create(code=code, name=name or code)


def make_market(base="BTC", quote="USDT", *, maker_bps="10", taker_bps="20",
                min_quantity="0.0001", min_notional="1"):
    if isinstance(base, str):
        base, _ = Asset.objects.get_or_create(code=base, defaults={"name": base})
    if isinstance(quote, str):
        quote, _ = Asset.objects.get_or_create(
            code=quote, defaults={"name": quote}
        )
    return Market.objects.create(
        symbol=f"{base.code}-{quote.code}",
        base_asset=base,
        quote_asset=quote,
        maker_fee_bps=Decimal(maker_bps),
        taker_fee_bps=Decimal(taker_bps),
        min_quantity=Decimal(min_quantity),
        min_notional=Decimal(min_notional),
    )


def credit(user, asset, amount):
    """Give a user simulated funds: genesis -> sim-bank -> user.

    The bank is lazily funded by a balanced mint from the ``sim-genesis``
    equity account (negative supply source), identical to what
    ``seed_simulated`` does, so reconciliation passes in tests.
    """
    from django.conf import settings
    from apps.accounts.services import get_or_create_balance
    from apps.ledger.services import mint_entries

    amount = Decimal(amount)
    bank, _ = get_or_create_system_user(settings.SIM_BANK_USERNAME)
    genesis, _ = get_or_create_system_user(settings.SIM_GENESIS_USERNAME)
    treasury = Decimal("1000000000")

    bank_bal = get_or_create_balance(bank, asset)
    genesis_bal = get_or_create_balance(genesis, asset, is_equity=True)
    minted = LedgerEntry.objects.filter(
        user=genesis, asset=asset
    ).exists()
    if not minted:
        # Initial simulated mint for this asset.
        bank_bal.available += treasury
        genesis_bal.available -= treasury
        Balance.objects.bulk_update(
            [bank_bal, genesis_bal],
            ["available", "frozen", "updated_at"],
        )
        post_event(
            LedgerEvent.EventType.DEPOSIT,
            mint_entries(bank, genesis, asset, treasury,
                         bank_bal, genesis_bal),
            note="test bank pre-mint",
        )
        bank_bal.refresh_from_db()
        genesis_bal.refresh_from_db()

    bank_bal.available -= amount
    bank_bal.save()
    bal, _ = Balance.objects.get_or_create(
        user=user, asset=asset,
        defaults={"available": Decimal("0"), "frozen": Decimal("0")},
    )
    bal.available += amount
    bal.save()
    post_event(
        LedgerEvent.EventType.DEPOSIT,
        [
            {
                "user": bank, "asset": asset,
                "subaccount": LedgerEntry.SubAccount.AVAILABLE,
                "amount": -amount, "balance_after": bank_bal.available,
            },
            {
                "user": user, "asset": asset,
                "subaccount": LedgerEntry.SubAccount.AVAILABLE,
                "amount": amount, "balance_after": bal.available,
            },
        ],
        note="test credit",
    )
    return bal


def balances(user):
    return {
        b.asset.code: (Decimal(b.available), Decimal(b.frozen))
        for b in Balance.objects.filter(user=user)
    }
