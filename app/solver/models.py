"""Rule language: CSP models built over finite integer domains.

A model is made of:

* variables with finite integer domains;
* binary constraints given by an explicit relation (allowed or forbidden
  pairs) or by an arithmetic comparison;
* the ``all_different`` global constraint.

The model is a pure, validated data structure. It performs no inference; the
propagation kernel lives in :mod:`app.solver.propagate`.
"""

from __future__ import annotations

from collections.abc import Iterable
from enum import Enum

from pydantic import BaseModel, Field, field_validator, model_validator


class ComparisonOp(str, Enum):
    EQ = "eq"
    NE = "ne"
    LT = "lt"
    LE = "le"
    GT = "gt"
    GE = "ge"


_COMPARISONS = {
    ComparisonOp.EQ: lambda a, b: a == b,
    ComparisonOp.NE: lambda a, b: a != b,
    ComparisonOp.LT: lambda a, b: a < b,
    ComparisonOp.LE: lambda a, b: a <= b,
    ComparisonOp.GT: lambda a, b: a > b,
    ComparisonOp.GE: lambda a, b: a >= b,
}


class RelationKind(str, Enum):
    """How a binary relation is specified."""

    ALLOWED = "allowed"
    FORBIDDEN = "forbidden"
    COMPARISON = "comparison"


class BinaryRelation(BaseModel, frozen=True):
    """A binary relation over two variables.

    * ``allowed``: ``pairs`` lists exactly the satisfying assignments;
    * ``forbidden``: ``pairs`` lists the unsatisfying assignments, every
      other pair is allowed;
    * ``comparison``: ``op`` names an arithmetic comparison, ``pairs`` must
      be empty.
    """

    kind: RelationKind
    pairs: frozenset[tuple[int, int]] = frozenset()
    op: ComparisonOp | None = None

    @model_validator(mode="after")
    def _validate(self) -> "BinaryRelation":
        if self.kind is RelationKind.COMPARISON:
            if self.op is None:
                raise ValueError("comparison relation requires an op")
            if self.pairs:
                raise ValueError("comparison relation must not list pairs")
        elif self.op is not None:
            raise ValueError(f"{self.kind.value} relation must not set op")
        return self

    def holds(self, a: int, b: int) -> bool:
        if self.kind is RelationKind.COMPARISON:
            return _COMPARISONS[self.op](a, b)  # type: ignore[index]
        if self.kind is RelationKind.ALLOWED:
            return (a, b) in self.pairs
        return (a, b) not in self.pairs

    def supports(self, a: int, b_values: Iterable[int]) -> set[int]:
        """Values of the second variable supported by value ``a``."""
        return {b for b in b_values if self.holds(a, b)}


class BinaryConstraint(BaseModel, frozen=True):
    left: str
    right: str
    relation: BinaryRelation

    @property
    def id(self) -> str:
        return f"{self.left}->{self.right}"


class CSPModel(BaseModel):
    """A finite-domain CSP instance."""

    name: str = Field(min_length=1)
    domains: dict[str, list[int]]
    binary_constraints: list[BinaryConstraint] = Field(default_factory=list)
    all_different: list[list[str]] = Field(default_factory=list)

    @field_validator("domains")
    @classmethod
    def _domains_nonempty(cls, value: dict[str, list[int]]) -> dict[str, list[int]]:
        if not value:
            raise ValueError("model needs at least one variable")
        for variable, domain in value.items():
            if not domain:
                raise ValueError(f"variable {variable!r} has an empty domain")
            if len(domain) != len(set(domain)):
                raise ValueError(f"variable {variable!r} has duplicate domain values")
        return value

    @model_validator(mode="after")
    def _validate_references(self) -> "CSPModel":
        known = set(self.domains)
        for constraint in self.binary_constraints:
            if constraint.left not in known:
                raise ValueError(f"unknown variable {constraint.left!r}")
            if constraint.right not in known:
                raise ValueError(f"unknown variable {constraint.right!r}")
            if constraint.left == constraint.right:
                raise ValueError("binary constraint needs two distinct variables")
        for group_index, group in enumerate(self.all_different):
            if len(group) != len(set(group)):
                raise ValueError(f"all_different[{group_index}] has duplicates")
            for variable in group:
                if variable not in known:
                    raise ValueError(f"unknown variable {variable!r} in all_different")
        return self

    def variable_count(self) -> int:
        return len(self.domains)

    def constraint_count(self) -> int:
        return len(self.binary_constraints) + len(self.all_different)
