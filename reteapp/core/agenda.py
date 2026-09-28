"""Conflict-resolution agenda.

Ordering (deterministic and auditable):

1. ``salience`` descending (higher priority first);
2. ``sequence`` ascending (first activated, first fired - the stable Rete
   conflict-resolution convention);
3. ``(rule_name, fact-id tuple)`` as the final stable-key tie-break, so two
   runs over identical inputs always produce identical firing order even if
   clocks or dict hashing differ.

The heap supports **lazy invalidation**: retracting a fact removes every
activation that depends on it by marking its heap entry dead; dead entries
are discarded when they reach the top. Each activation's ``stable_key`` is
exposed in traces and API responses.
"""

from __future__ import annotations

import heapq
import itertools
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class Activation:
    rule_name: str
    salience: int
    token_key: tuple[int, ...]
    wme_ids: tuple[int, ...]
    bindings: dict[str, Any]
    sequence: int

    @property
    def stable_key(self) -> str:
        joined = "-".join(str(i) for i in self.wme_ids)
        return f"{self.rule_name}[{joined}]"


@dataclass
class _HeapEntry:
    salience: int
    sequence: int
    rule_name: str
    token_key: tuple[int, ...]
    alive: bool = True

    def heap_tuple(self) -> tuple:
        return (
            -self.salience,
            self.sequence,
            self.rule_name,
            self.token_key,
            id(self),
        )


@dataclass
class Agenda:
    _counter: itertools.count = field(default_factory=itertools.count)
    _entries: dict[tuple[str, tuple[int, ...]], _HeapEntry] = field(default_factory=dict)
    _heap: list[tuple] = field(default_factory=list)
    # Refraction support: keys of activations that already fired while their
    # token stayed alive.
    _fired: set[tuple[str, tuple[int, ...]]] = field(default_factory=set)

    def __len__(self) -> int:
        return sum(1 for e in self._entries.values() if e.alive)

    def add(
        self,
        rule_name: str,
        salience: int,
        wme_ids: tuple[int, ...],
        bindings: dict[str, Any],
        *,
        refraction: bool,
    ) -> Activation | None:
        """Insert an activation. Returns ``None`` when suppressed by refraction."""

        key = (rule_name, wme_ids)
        existing = self._entries.get(key)
        if existing is not None and existing.alive:
            return self._to_activation(existing, bindings)
        if refraction and key in self._fired:
            return None
        entry = _HeapEntry(
            salience=salience,
            sequence=next(self._counter),
            rule_name=rule_name,
            token_key=wme_ids,
        )
        self._entries[key] = entry
        heapq.heappush(self._heap, entry.heap_tuple())
        return self._to_activation(entry, bindings)

    def peek_ordered(self) -> list[Activation]:
        """All live activations in firing order (for queries/tests)."""

        live = [e for e in self._entries.values() if e.alive]
        live.sort(key=lambda e: (-e.salience, e.sequence, e.rule_name, e.token_key))
        # Bindings are not stored on heap entries; callers that need payloads
        # use ``snapshot`` from the engine, which joins on terminal tokens.
        return [
            Activation(
                rule_name=e.rule_name,
                salience=e.salience,
                token_key=e.token_key,
                wme_ids=e.token_key,
                bindings={},
                sequence=e.sequence,
            )
            for e in live
        ]

    def pop(self, bindings_provider) -> Activation | None:
        """Pop the highest-priority live activation.

        ``bindings_provider(rule_name, token_key)`` supplies the variable
        bindings kept by the network terminal memory.
        """

        while self._heap:
            neg_sal, seq, rule_name, token_key, entry_id = heapq.heappop(self._heap)
            key = (rule_name, token_key)
            entry = self._entries.get(key)
            if entry is None or not entry.alive or id(entry) != entry_id:
                continue
            del self._entries[key]
            bindings = bindings_provider(rule_name, token_key) or {}
            return Activation(
                rule_name=rule_name,
                salience=entry.salience,
                token_key=token_key,
                wme_ids=token_key,
                bindings=bindings,
                sequence=seq,
            )
        return None

    def invalidate(self, rule_name: str, token_key: tuple[int, ...]) -> _HeapEntry | None:
        """Kill one activation (retract propagation). Returns the entry or None."""

        entry = self._entries.pop((rule_name, token_key), None)
        if entry is None:
            return None
        entry.alive = False
        return entry

    def mark_fired(self, rule_name: str, token_key: tuple[int, ...]) -> None:
        self._fired.add((rule_name, token_key))

    def unmark_fired(self, rule_name: str, token_key: tuple[int, ...]) -> None:
        self._fired.discard((rule_name, token_key))

    @staticmethod
    def _to_activation(entry: _HeapEntry, bindings: dict[str, Any]) -> Activation:
        return Activation(
            rule_name=entry.rule_name,
            salience=entry.salience,
            token_key=entry.token_key,
            wme_ids=entry.token_key,
            bindings=dict(bindings),
            sequence=entry.sequence,
        )
