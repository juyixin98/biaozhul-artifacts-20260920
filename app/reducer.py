"""Reversible projection: Vault events -> materialized business state.

Each event carries its full delta, so every transition is invertible:

* ``Deposited(who, amount)``  apply :  balance[who] += amount, deposited += amount
                              revert:  balance[who] -= amount, deposited -= amount
* ``Withdrawn(who, amount)``  apply :  balance[who] -= amount, withdrawn += amount
                              revert:  balance[who] += amount, withdrawn -= amount

Rolling a whole block back means applying the inverse of every event in
**reverse log order**, so a rollback always returns state to exactly what it
was before the block was applied (assuming the event stream was consistent,
which on-chain validity guarantees).
"""
from __future__ import annotations

from .storage import EventRow, Storage

TOTAL_DEPOSITED = "deposited"
TOTAL_WITHDRAWN = "withdrawn"

# Signed per-event delta: (balance_delta[who], total_delta[key])
_EFFECT = {
    "Deposited": (+1, TOTAL_DEPOSITED, +1),
    "Withdrawn": (-1, TOTAL_WITHDRAWN, +1),
}


def _apply(store: Storage, ev: EventRow, sign: int) -> None:
    if ev.event_name not in _EFFECT:
        raise ValueError(f"unknown event {ev.event_name!r}")
    bal_sign, total_key, total_sign = _EFFECT[ev.event_name]
    store.balance_add(ev.who, sign * bal_sign * ev.amount)
    store.total_add(total_key, sign * total_sign * ev.amount)


def apply_event(store: Storage, ev: EventRow) -> None:
    _apply(store, ev, +1)


def revert_event(store: Storage, ev: EventRow) -> None:
    _apply(store, ev, -1)


def apply_block(store: Storage, events: list[EventRow]) -> None:
    """Apply events in ascending (tx_index, log_index) order."""
    for ev in events:
        apply_event(store, ev)


def revert_block(store: Storage, events: list[EventRow]) -> None:
    """Undo a block: inverse of each event in descending log order."""
    for ev in reversed(events):
        revert_event(store, ev)
