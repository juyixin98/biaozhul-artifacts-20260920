"""Rule language: state predicates and boolean combinators.

A condition is one of::

    {"fluent": "x", "op": ">=", "value": 3}        # comparison predicate
    {"all": [cond, cond, ...]}                     # conjunction (empty -> true)
    {"any": [cond, ...]}                           # disjunction (empty -> false)
    {"not": cond}                                  # negation

All values are parsed as exact rationals; comparisons never use floats.
"""

from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction
from typing import Mapping

_OPS = {
    "==": lambda a, b: a == b,
    "!=": lambda a, b: a != b,
    "<": lambda a, b: a < b,
    "<=": lambda a, b: a <= b,
    ">": lambda a, b: a > b,
    ">=": lambda a, b: a >= b,
}


class Condition:
    """Base predicate. Subclasses implement ``evaluate`` and ``describe``."""

    def evaluate(self, state: Mapping[str, Fraction]) -> bool:
        raise NotImplementedError

    def describe(self) -> str:
        raise NotImplementedError


@dataclass(frozen=True)
class Comparison(Condition):
    fluent: str
    op: str
    value: Fraction

    def evaluate(self, state: Mapping[str, Fraction]) -> bool:
        return _OPS[self.op](state[self.fluent], self.value)

    def describe(self) -> str:
        return f"{self.fluent} {self.op} {self.value}"


@dataclass(frozen=True)
class All(Condition):
    children: tuple[Condition, ...]

    def evaluate(self, state: Mapping[str, Fraction]) -> bool:
        return all(c.evaluate(state) for c in self.children)

    def describe(self) -> str:
        return "(" + " AND ".join(c.describe() for c in self.children) + ")"


@dataclass(frozen=True)
class Any(Condition):
    children: tuple[Condition, ...]

    def evaluate(self, state: Mapping[str, Fraction]) -> bool:
        return any(c.evaluate(state) for c in self.children)

    def describe(self) -> str:
        return "(" + " OR ".join(c.describe() for c in self.children) + ")"


@dataclass(frozen=True)
class Not(Condition):
    child: Condition

    def evaluate(self, state: Mapping[str, Fraction]) -> bool:
        return not self.child.evaluate(state)

    def describe(self) -> str:
        return f"NOT {self.child.describe()}"


def condition_from_dict(data: dict, known_fluents: set[str]) -> Condition:
    if not isinstance(data, dict) or len(data) != 1:
        raise ValueError(
            "condition must be an object with exactly one key: "
            "fluent | all | any | not"
        )
    (key, body), = data.items()
    if key == "fluent":
        if not isinstance(body, dict):
            raise ValueError("'fluent' condition must be an object")
        fid = body.get("id", body.get("fluent"))
        if fid not in known_fluents:
            raise ValueError(f"condition references undeclared fluent {fid!r}")
        op = body.get("op", "==")
        if op not in _OPS:
            raise ValueError(f"unknown comparison op {op!r}; allowed: {sorted(_OPS)}")
        try:
            value = Fraction(body.get("value", 0))
        except (TypeError, ValueError, ZeroDivisionError) as exc:
            raise ValueError(f"invalid comparison value for fluent {fid!r}") from exc
        return Comparison(fluent=fid, op=op, value=value)
    if key == "all":
        if not isinstance(body, list):
            raise ValueError("'all' must be a list of conditions")
        return All(tuple(condition_from_dict(c, known_fluents) for c in body))
    if key == "any":
        if not isinstance(body, list):
            raise ValueError("'any' must be a list of conditions")
        return Any(tuple(condition_from_dict(c, known_fluents) for c in body))
    if key == "not":
        return Not(condition_from_dict(body, known_fluents))
    raise ValueError(f"unknown condition key {key!r}")
