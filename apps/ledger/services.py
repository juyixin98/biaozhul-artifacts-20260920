"""Append-only double-entry ledger writer.

Every event consists of signed entries (per asset) that sum to zero:

* a movement ``available -> frozen`` is one negative AVAILABLE entry and one
  positive FROZEN entry on the same user/asset;
* a trade is a set of four (or six with fees) user/asset/subaccount moves
  between taker, maker and the fee account;
* simulated deposits are balanced against the system ``sim-bank`` user.

``post_event`` is the *only* code allowed to create ledger rows, and it is
always called inside the same DB transaction as the balance mutation, so a
crash cannot leave balances changed without their audit entries or vice
versa.
"""
from decimal import Decimal

from apps.ledger.models import LedgerEntry, LedgerEvent


def _entry(event, *, user, asset, subaccount, amount, balance_after):
    return LedgerEntry(
        event=event,
        user=user,
        asset=asset,
        subaccount=subaccount,
        amount=amount,
        balance_after=balance_after,
    )


def post_event(type, entries, *, order=None, trade=None, note=""):
    """Persist one balanced ledger event.

    ``entries`` is an iterable of dicts::

        {"user", "asset", "subaccount", "amount", "balance_after"}

    The per-asset sum is asserted zero to guard against construction bugs.
    """
    entries = list(entries)
    sums = {}
    for e in entries:
        key = e["asset"].id
        sums[key] = sums.get(key, Decimal("0")) + Decimal(e["amount"])
    for asset_id, total in sums.items():
        if total != 0:
            raise AssertionError(
                f"ledger event {type} not balanced for asset {asset_id}: "
                f"sum={total}"
            )

    event = LedgerEvent.objects.create(
        type=type, order=order, trade=trade, note=note
    )
    LedgerEntry.objects.bulk_create(
        [
            _entry(
                event,
                user=e["user"],
                asset=e["asset"],
                subaccount=e["subaccount"],
                amount=Decimal(e["amount"]),
                balance_after=Decimal(e["balance_after"]),
            )
            for e in entries
        ]
    )
    return event


def mint_entries(bank_user, genesis_user, asset, amount, bank_balance,
                 genesis_balance):
    """Balanced entries for a simulated mint: genesis equity -> sim-bank."""
    return [
        {
            "user": genesis_user,
            "asset": asset,
            "subaccount": LedgerEntry.SubAccount.AVAILABLE,
            "amount": -amount,
            "balance_after": genesis_balance.available,
        },
        {
            "user": bank_user,
            "asset": asset,
            "subaccount": LedgerEntry.SubAccount.AVAILABLE,
            "amount": amount,
            "balance_after": bank_balance.available,
        },
    ]


def freeze_entries(user, asset, amount, balance):
    """Ledger lines for available -> frozen (already applied to balance)."""
    return [
        {
            "user": user,
            "asset": asset,
            "subaccount": LedgerEntry.SubAccount.AVAILABLE,
            "amount": -amount,
            "balance_after": balance.available,
        },
        {
            "user": user,
            "asset": asset,
            "subaccount": LedgerEntry.SubAccount.FROZEN,
            "amount": amount,
            "balance_after": balance.frozen,
        },
    ]


def release_entries(user, asset, amount, balance):
    """Ledger lines for frozen -> available (already applied to balance)."""
    return [
        {
            "user": user,
            "asset": asset,
            "subaccount": LedgerEntry.SubAccount.FROZEN,
            "amount": -amount,
            "balance_after": balance.frozen,
        },
        {
            "user": user,
            "asset": asset,
            "subaccount": LedgerEntry.SubAccount.AVAILABLE,
            "amount": amount,
            "balance_after": balance.available,
        },
    ]


def trade_entries(
    *,
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
    taker_balances,
    maker_balances,
    fee_balances,
):
    """Build the balanced entry list for one fill (balances already updated)."""
    b, q = base_asset, quote_asset
    E = []

    def line(user, asset, sub, amount, bal):
        E.append(
            {
                "user": user,
                "asset": asset,
                "subaccount": sub,
                "amount": amount,
                "balance_after": getattr(bal, "available" if sub == "AVAILABLE" else "frozen"),
            }
        )

    AV = "AVAILABLE"
    FR = "FROZEN"
    if side == "BUY":
        # taker buyer
        line(taker, q, FR, -cost, taker_balances[(taker.id, q.id)])
        line(taker, b, AV, quantity - taker_fee,
             taker_balances[(taker.id, b.id)])
        # maker seller
        line(maker, b, FR, -quantity, maker_balances[(maker.id, b.id)])
        line(maker, q, AV, cost - maker_fee,
             maker_balances[(maker.id, q.id)])
        # fees: buyer fee in base, seller fee in quote
        line(fee_user, b, AV, taker_fee, fee_balances[(fee_user.id, b.id)])
        line(fee_user, q, AV, maker_fee, fee_balances[(fee_user.id, q.id)])
    else:
        # taker seller
        line(taker, b, FR, -quantity, taker_balances[(taker.id, b.id)])
        line(taker, q, AV, cost - taker_fee,
             taker_balances[(taker.id, q.id)])
        # maker buyer
        line(maker, q, FR, -cost, maker_balances[(maker.id, q.id)])
        line(maker, b, AV, quantity - maker_fee,
             maker_balances[(maker.id, b.id)])
        # fees: seller fee in quote, buyer fee in base
        line(fee_user, q, AV, taker_fee, fee_balances[(fee_user.id, q.id)])
        line(fee_user, b, AV, maker_fee, fee_balances[(fee_user.id, b.id)])
    return E
