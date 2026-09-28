"""Rule language: a small positive relational algebra.

Supported operators (positive queries only -- no negation / difference):

* ``relation``  : base table reference pinned to an explicit input version
* ``select``    : selection by a conjunction of comparisons
* ``project``   : column projection
* ``join``      : (theta) join on a conjunction of column-to-column comparisons
* ``union``     : bag union (UNION ALL) by default, or set union when distinct

The external representation is plain JSON (see ``parse_plan``). Parsing only
checks *well-formedness*; schema/type compatibility is validated later by the
planner against a specific input version, so the two responsibilities stay
separate.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

from .errors import PlanError

ComparisonOp = Literal["=", "!=", "<", "<=", ">", ">="]
OPS: frozenset[str] = frozenset({"=", "!=", "<", "<=", ">", ">="})
NodeType = Literal["relation", "select", "project", "join", "union"]


@dataclass(frozen=True)
class RelationRef:
    name: str
    version: str
    alias: str | None = None
    kind: str = field(default="relation", init=False)

    @property
    def binding(self) -> str:
        """Name used to qualify columns (needed for self-joins)."""
        return self.alias or self.name


@dataclass(frozen=True)
class Comparison:
    op: ComparisonOp
    left: str  # qualified or bare column
    right: str | None = None  # column when comparing two columns
    literal: Any = None  # constant when comparing a column to a literal

    @property
    def is_column_predicate(self) -> bool:
        return self.right is not None


@dataclass(frozen=True)
class Select:
    child: "Node"
    predicates: tuple[Comparison, ...]
    kind: str = field(default="select", init=False)


@dataclass(frozen=True)
class Project:
    child: "Node"
    columns: tuple[str, ...]
    kind: str = field(default="project", init=False)


@dataclass(frozen=True)
class Join:
    left: "Node"
    right: "Node"
    predicates: tuple[Comparison, ...]
    kind: str = field(default="join", init=False)


@dataclass(frozen=True)
class Union:
    left: "Node"
    right: "Node"
    kind: str = field(default="union", init=False)


Node = RelationRef | Select | Project | Join | Union


# --------------------------------------------------------------------------- #
# Parsing
# --------------------------------------------------------------------------- #
def _require(condition: bool, message: str) -> None:
    if not condition:
        raise PlanError(message)


def _parse_comparison(raw: dict[str, Any], *, allow_literal: bool) -> Comparison:
    _require(isinstance(raw, dict), "predicate must be an object")
    op = raw.get("op")
    _require(op in OPS, f"unsupported comparison op {op!r}; expected one of {sorted(OPS)}")
    left = raw.get("left")
    _require(isinstance(left, str) and left, "predicate 'left' must name a column")

    if "right" in raw and raw["right"] is not None:
        right = raw["right"]
        _require(isinstance(right, str) and right, "predicate 'right' must name a column")
        return Comparison(op=op, left=left, right=right)  # type: ignore[arg-type]

    if allow_literal:
        _require("literal" in raw, "predicate needs either 'right' (column) or 'literal'")
        literal = raw["literal"]
        _require(
            literal is None or isinstance(literal, (str, int, float, bool)),
            "predicate 'literal' must be a scalar (str/number/bool/null)",
        )
        return Comparison(op=op, left=left, literal=literal)  # type: ignore[arg-type]

    raise PlanError("join predicates compare two columns; 'literal' is not allowed here")


def _parse_predicates(raw: Any, *, allow_literal: bool) -> tuple[Comparison, ...]:
    if raw is None:
        return ()
    _require(isinstance(raw, list) and raw, "'predicates' must be a non-empty list")
    return tuple(_parse_comparison(p, allow_literal=allow_literal) for p in raw)


def _parse_columns(raw: Any) -> tuple[str, ...]:
    _require(isinstance(raw, list) and raw, "'columns' must be a non-empty list of names")
    columns = tuple(raw)
    _require(
        all(isinstance(c, str) and c for c in columns),
        "every projected column must be a non-empty string",
    )
    _require(len(columns) == len(set(columns)), "projected columns must be unique")
    return columns  # type: ignore[return-value]


def parse_plan(raw: Any) -> Node:
    """Parse a JSON-friendly dict into a typed plan tree or raise PlanError."""
    _require(isinstance(raw, dict), "plan must be an object")
    kind = raw.get("op")
    _require(isinstance(kind, str), "plan node needs an 'op' string")

    if kind == "relation":
        name, version = raw.get("name"), raw.get("version")
        _require(isinstance(name, str) and name, "relation needs a non-empty 'name'")
        _require(isinstance(version, str) and version, "relation must pin an explicit 'version'")
        alias = raw.get("alias")
        _require(alias is None or (isinstance(alias, str) and alias), "relation 'alias' must be a non-empty string")
        return RelationRef(name=name, version=version, alias=alias)

    if kind == "select":
        child = parse_plan(raw.get("child"))
        preds = _parse_predicates(raw.get("predicates"), allow_literal=True)
        return Select(child=child, predicates=preds)

    if kind == "project":
        child = parse_plan(raw.get("child"))
        return Project(child=child, columns=_parse_columns(raw.get("columns")))

    if kind == "join":
        left, right = parse_plan(raw.get("left")), parse_plan(raw.get("right"))
        preds = _parse_predicates(raw.get("predicates"), allow_literal=False)
        return Join(left=left, right=right, predicates=preds)

    if kind == "union":
        left, right = parse_plan(raw.get("left")), parse_plan(raw.get("right"))
        if "distinct" in raw:
            # Positive relational algebra here is bag-based: union is additive,
            # which is exactly what lets duplicate derivations show up in the
            # polynomial. DISTINCT semantics are deliberately out of scope.
            raise PlanError("'distinct' on union is not supported; provenance union is bag-based")
        return Union(left=left, right=right)

    raise PlanError(
        f"unknown operator {kind!r}; supported: relation, select, project, join, union"
    )
