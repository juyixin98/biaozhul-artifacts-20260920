"""Mutable planning state: ground-fact set plus finite shared resources.

A *state* is what primitive preconditions are evaluated against and what
primitive effects transform.  Resources are modelled explicitly rather than as
plain facts so that "capacity 1 shared by two siblings" conflicts are detected
by the partial-order feasibility check rather than by name matching.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .models import Condition, Primitive


class ConditionError(Exception):
    """Raised when a condition references an undeclared resource."""


@dataclass
class State:
    facts: frozenset[tuple[str, ...]]
    # resource name -> (capacity, held)
    resources: dict[str, list[int]] = field(default_factory=dict)

    @classmethod
    def empty(cls) -> "State":
        return cls(facts=frozenset())

    @classmethod
    def from_problem(cls, problem, domain) -> "State":
        facts = frozenset(tuple(f) for f in problem.initial_facts)
        resources: dict[str, list[int]] = {}
        for rname, res in domain.resources.items():
            held = problem.initial_resources.get(rname, 0)
            resources[rname] = [res.capacity, held]
        # Allow problem files to declare resources absent from the domain block
        for rname, held in problem.initial_resources.items():
            resources.setdefault(rname, [max(held, 1), held])
        return cls(facts=facts, resources=resources)

    # -- copies -----------------------------------------------------------
    def snapshot(self) -> "State":
        return State(
            facts=self.facts,
            resources={k: list(v) for k, v in self.resources.items()},
        )

    def clone(self) -> "State":
        return self.snapshot()

    # -- condition evaluation --------------------------------------------
    def _check_resource(self, cond: Condition) -> bool:
        if cond.name not in self.resources:
            raise ConditionError(
                f"condition references undeclared resource {cond.name!r}"
            )
        capacity, held = self.resources[cond.name]
        free = capacity - held
        if cond.kind == "avail":
            return free >= cond.amount
        # bound: fewer than `amount` units are free
        return free < cond.amount

    def satisfies(self, cond: Condition) -> bool:
        if cond.kind == "fact":
            return cond.literal_key() in self.facts
        if cond.kind == "not":
            return cond.literal_key() not in self.facts
        return self._check_resource(cond)

    def satisfies_all(self, conds: list[Condition]) -> tuple[bool, Condition | None]:
        for c in conds:
            if not self.satisfies(c):
                return False, c
        return True, None

    def lookup_values(self, name: str, pattern: tuple[str, ...]) -> list[str]:
        """All final terms of facts shaped ``(name, *pattern, value)``.

        Supports the rule language's deterministic ``bind`` guard: a unique
        match derives one auxiliary variable; zero or many matches mean the
        guard cannot uniquely commit and it is rejected (not guessed).
        """
        prefix = (name, *pattern)
        return [
            f[-1]
            for f in self.facts
            if len(f) == len(prefix) + 1 and tuple(f[:-1]) == prefix
        ]

    # -- primitive simulation --------------------------------------------
    def can_execute(self, prim: Primitive) -> tuple[bool, str | None]:
        """Real precondition check against the current state.

        Returns (True, None) when executable, otherwise (False, reason) where
        reason distinguishes a failed state-literal from resource exhaustion.
        """
        ok, bad = self.satisfies_all(prim.precondition)
        if not ok:
            assert bad is not None
            if bad.kind in ("avail", "bound"):
                capacity, held = self.resources.get(bad.name, [0, 0])
                return (
                    False,
                    f"resource {bad.name!r}: need {bad.amount} free, "
                    f"capacity={capacity} held={held}",
                )
            return False, f"required literal absent: {bad.literal_key()}"
        # Effects must also be *applicable*: cannot reserve more than free.
        for res in prim.effect.reserve:
            rname, amount = res["resource"], int(res["amount"])
            if rname not in self.resources:
                return False, f"reserve references undeclared resource {rname!r}"
            capacity, held = self.resources[rname]
            if capacity - held < amount:
                return (
                    False,
                    f"resource {rname!r} exhausted on reserve: need {amount}, "
                    f"free={capacity - held}",
                )
        return True, None

    def apply(self, prim: Primitive) -> "State":
        """Return a new state (immutable update) after applying ``prim``."""
        facts = set(self.facts)
        for removed in prim.effect.remove:
            facts.discard(tuple(removed))
        for added in prim.effect.add:
            facts.add(tuple(added))
        resources = {k: list(v) for k, v in self.resources.items()}
        for res in prim.effect.reserve:
            resources[res["resource"]][1] += int(res["amount"])
        for rel in prim.effect.release:
            rname, amount = rel[0], int(rel[1])
            if rname in resources:
                resources[rname][1] = max(0, resources[rname][1] - amount)
        return State(facts=frozenset(facts), resources=resources)

    def fact_list(self) -> list[list[str]]:
        return [list(f) for f in sorted(self.facts)]

    def signature(self) -> tuple:
        """Structural signature for recursion/cycle detection.

        Two states with the same facts and resource holdings are
        indistinguishable to preconditions and effects, so an identical task
        frame recurring under an identical signature cannot progress.
        """
        return (
            tuple(sorted(self.facts)),
            tuple(sorted((k, v[0], v[1]) for k, v in self.resources.items())),
        )
