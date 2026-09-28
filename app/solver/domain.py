"""Mutable domain store with an explicit trail.

Domains are mutated during propagation and restored on backtracking by
replaying the trail. Every pruning is recorded so that :meth:`restore`
recovers the exact previous domain state.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping

TrailEntry = tuple[str, int]


class EmptyDomain(Exception):
    """Raised when a pruning would empty a variable domain."""

    def __init__(self, variable: str):
        super().__init__(f"domain of variable {variable!r} became empty")
        self.variable = variable


class DomainStore:
    """Holds the current finite domain of every variable."""

    def __init__(self, domains: Mapping[str, Iterable[int]]):
        self._domains: dict[str, set[int]] = {
            variable: set(values) for variable, values in domains.items()
        }

    def variables(self) -> list[str]:
        return list(self._domains)

    def domain(self, variable: str) -> set[int]:
        return self._domains[variable]

    def freeze(self) -> dict[str, frozenset[int]]:
        return {variable: frozenset(values) for variable, values in self._domains.items()}

    def is_singleton(self, variable: str) -> bool:
        return len(self._domains[variable]) == 1

    def all_singletons(self) -> bool:
        return all(len(values) == 1 for values in self._domains.values())

    def prune(self, variable: str, value: int, trail: list[TrailEntry]) -> None:
        values = self._domains[variable]
        if value not in values:
            return
        values.remove(value)
        trail.append((variable, value))
        if not values:
            raise EmptyDomain(variable)

    def assign(self, variable: str, value: int, trail: list[TrailEntry]) -> list[int]:
        """Assign a variable, pruning every other value. Returns removed values."""
        removed = sorted(self._domains[variable] - {value})
        for other in removed:
            self.prune(variable, other, trail)
        return removed

    @staticmethod
    def restore(entries: list[TrailEntry], store: "DomainStore") -> None:
        """Re-add every trailed value, in reverse pruning order."""
        for variable, value in reversed(entries):
            store._domains[variable].add(value)
