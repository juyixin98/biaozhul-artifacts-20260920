"""Reversible business effects of Ledger events.

Each event type has a forward effect (applied when the event joins the
canonical chain) and an inverse effect (applied when a reorg detaches its
block). The materialized state (account balances) is therefore a pure fold
over the canonical event log and can always be reconstructed by replay.
"""

from __future__ import annotations

from typing import Callable

# A decoded event as stored/handled internally.
#   name      : "Deposited" | "Withdrawn" | "Transferred"
#   account   : checksummed primary account (depositor / withdrawer / sender)
#   to_account: checksummed recipient for Transferred, else None
#   amount    : unsigned 256-bit amount as a Python int
#   tag       : client correlation tag
DecodedEvent = dict

BalanceState = dict[str, int]

FORWARD_SIGNATURES: dict[str, tuple[str, list[str]]] = {
    "Deposited": ("Deposited(address,uint256,uint256)", ["uint256", "uint256"]),
    "Withdrawn": ("Withdrawn(address,uint256,uint256)", ["uint256", "uint256"]),
    "Transferred": (
        "Transferred(address,address,uint256,uint256)",
        ["uint256", "uint256"],
    ),
}


def apply_forward(balances: BalanceState, event: DecodedEvent) -> None:
    name = event["name"]
    account = event["account"]
    amount = event["amount"]
    if name == "Deposited":
        balances[account] = balances.get(account, 0) + amount
    elif name == "Withdrawn":
        new = balances.get(account, 0) - amount
        if new < 0:
            raise ValueError(f"inverse invariant violated: {account} balance went negative")
        balances[account] = new
    elif name == "Transferred":
        to = event["to_account"]
        assert to is not None
        sender = balances.get(account, 0) - amount
        if sender < 0:
            raise ValueError(f"inverse invariant violated: {account} balance went negative")
        balances[account] = sender
        balances[to] = balances.get(to, 0) + amount
    else:
        raise ValueError(f"unknown event {name!r}")


def apply_inverse(balances: BalanceState, event: DecodedEvent) -> None:
    """Exact inverse of :func:`apply_forward`.

    Rolling a block back means undoing its events in reverse log order.
    """
    name = event["name"]
    account = event["account"]
    amount = event["amount"]
    if name == "Deposited":
        new = balances.get(account, 0) - amount
        if new < 0:
            raise ValueError(f"rollback invariant violated: {account} balance went negative")
        balances[account] = new
    elif name == "Withdrawn":
        balances[account] = balances.get(account, 0) + amount
    elif name == "Transferred":
        to = event["to_account"]
        assert to is not None
        recipient = balances.get(to, 0) - amount
        if recipient < 0:
            raise ValueError(f"rollback invariant violated: {to} balance went negative")
        balances[to] = recipient
        balances[account] = balances.get(account, 0) + amount
    else:
        raise ValueError(f"unknown event {name!r}")


EffectFn = Callable[[BalanceState, DecodedEvent], None]

EFFECTS: dict[str, tuple[EffectFn, EffectFn]] = {
    name: (apply_forward, apply_inverse) for name in FORWARD_SIGNATURES
}
