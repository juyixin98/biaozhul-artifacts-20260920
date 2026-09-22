"""Balance mutation primitives.

Every balance change in the engine goes through these helpers while the
relevant rows are locked with ``SELECT ... FOR UPDATE`` inside a single
atomic transaction.  Freeze / release / settle are complementary operations
that always preserve total assets on the platform (the ledger records the
corresponding paired entries).
"""
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.db import transaction

from apps.accounts.models import Balance
from apps.common.decimals import ZERO

User = get_user_model()


class InsufficientFunds(Exception):
    pass


def get_or_create_system_user(username):
    """Return (user, created) for a system account (fee/bank), idempotent."""
    return User.objects.get_or_create(
        username=username,
        defaults={"is_active": True, "is_staff": False},
    )


def get_or_create_balance(user, asset, *, is_equity=False) -> Balance:
    balance, _ = Balance.objects.get_or_create(
        user=user, asset=asset,
        defaults={
            "available": ZERO, "frozen": ZERO, "is_equity": is_equity
        },
    )
    return balance


def lock_balances(pairs):
    """Lock the given iterable of (user, asset) rows, keyed by (uid, aid).

    Rows are created if missing and then locked in a deterministic order
    (user id, asset id) to prevent AB/BA deadlocks between concurrent
    settlements.
    """
    pairs = sorted(set(pairs), key=lambda ua: (ua[0].id, ua[1].id))
    if not pairs:
        return {}
    for user, asset in pairs:
        get_or_create_balance(user, asset)
    locked = {}
    qs = Balance.objects.select_for_update()
    for user, asset in pairs:
        locked[(user.id, asset.id)] = qs.get(user=user, asset=asset)
    return locked


def freeze(balance: Balance, amount: Decimal):
    amount = Decimal(amount)
    if amount < 0:
        raise ValueError("cannot freeze a negative amount")
    if balance.available < amount:
        raise InsufficientFunds(
            f"available {balance.available} < freeze {amount} {balance.asset}"
        )
    balance.available -= amount
    balance.frozen += amount
    balance.save(update_fields=["available", "frozen", "updated_at"])


def release(balance: Balance, amount: Decimal):
    """Move *amount* from frozen back to available (unfilled order cancel)."""
    amount = Decimal(amount)
    if amount < 0:
        raise ValueError("cannot release a negative amount")
    if balance.frozen < amount:
        raise InsufficientFunds(
            f"frozen {balance.frozen} < release {amount} {balance.asset}"
        )
    balance.frozen -= amount
    balance.available += amount
    balance.save(update_fields=["available", "frozen", "updated_at"])


def settle_fill(
    *,
    taker_balances,
    maker_balances,
    fee_balances,
    taker,
    maker,
    fee_user,
    side,
    base_asset,
    quote_asset,
    cost,
    quantity,
    taker_fee,
    maker_fee,
):
    """Apply one fill against already-locked balance rows.

    ``cost``     = quote amount changing hands (8 dp, HALF_UP).
    ``quantity`` = base amount changing hands (8 dp).
    Fees are charged in the asset each side *receives*: the buyer's fee in
    base, the seller's fee in quote.

    Taker BUY / maker SELL:
      taker frozen quote  -cost ; taker avail base  +(qty - taker_fee)
      maker frozen base   -qty  ; maker avail quote +(cost - maker_fee)
      fee account avail base  += taker_fee  (buyer's fee)
      fee account avail quote += maker_fee  (seller's fee)

    Taker SELL / maker BUY is the mirror image.

    Total available+frozen over the platform is unchanged by every line:
    a move out of one party's frozen column lands in another party's
    available column, and both fees come out of the receiver's gross.
    """
    cost = Decimal(cost)
    quantity = Decimal(quantity)
    taker_fee = Decimal(taker_fee)
    maker_fee = Decimal(maker_fee)

    bk, qk = base_asset.id, quote_asset.id
    if side == "BUY":
        # --- taker (buyer): fee in base received ---
        taker_balances[(taker.id, qk)].frozen -= cost
        taker_balances[(taker.id, bk)].available += quantity - taker_fee
        # --- maker (seller): fee in quote received ---
        maker_balances[(maker.id, bk)].frozen -= quantity
        maker_balances[(maker.id, qk)].available += cost - maker_fee
        # --- fees ---
        fee_balances[(fee_user.id, bk)].available += taker_fee
        fee_balances[(fee_user.id, qk)].available += maker_fee
    else:
        # --- taker (seller): fee in quote received ---
        taker_balances[(taker.id, bk)].frozen -= quantity
        taker_balances[(taker.id, qk)].available += cost - taker_fee
        # --- maker (buyer): fee in base received ---
        maker_balances[(maker.id, qk)].frozen -= cost
        maker_balances[(maker.id, bk)].available += quantity - maker_fee
        # --- fees ---
        fee_balances[(fee_user.id, qk)].available += taker_fee
        fee_balances[(fee_user.id, bk)].available += maker_fee

    rows = (
        list(taker_balances.values())
        + list(maker_balances.values())
        + list(fee_balances.values())
    )
    for row in rows:
        # CheckConstraints are the hard guarantee; this gives a clean error.
        if row.available < ZERO or row.frozen < ZERO:
            raise InsufficientFunds(
                f"settlement would overdraw {row.user} {row.asset}"
            )
    Balance.objects.bulk_update(rows, ["available", "frozen", "updated_at"])
    return rows
