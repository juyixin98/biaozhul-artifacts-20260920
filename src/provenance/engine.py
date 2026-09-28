"""Reasoning kernel: N[X]-annotated bag relational algebra.

An *annotated relation* maps each distinct output tuple to the :class:`Poly`
that is the sum of all its derivations. The operators are the provenance
semiring lifts:

* selection keeps a row's polynomial unchanged (it only filters),
* projection keeps the polynomial and **adds** rows that collapse together,
* join multiplies the two sides' polynomials,
* union adds the two sides' polynomials.

This module is deliberately pure: it knows nothing about HTTP or SQLite. It
receives already-resolved leaf relations and returns annotated results.
"""
from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction
from typing import Any

from .errors import PlanError, TypeRuleError
from .polynomial import Poly
from .rule_language import Comparison

# A scalar that may appear in a synthetic tuple.
Scalar = str | int | float | bool | None
RowKey = tuple[Scalar, ...]

# Tri-state truth for the single, explicitly supported NULL policy.
TRUE, FALSE, UNKNOWN = "true", "false", "unknown"


@dataclass(frozen=True)
class Attr:
    binding: str  # leaf alias (or relation name)
    name: str  # bare column name

    @property
    def label(self) -> str:
        return f"{self.binding}.{self.name}"


@dataclass(frozen=True)
class LeafRelation:
    """A resolved base relation ready to be annotated."""

    binding: str
    name: str
    columns: tuple[str, ...]
    # tuple_id -> tuple data; kept as items in insertion order.
    tuples: tuple[tuple[str, dict[str, Any]], ...]

    def variable(self, tuple_id: str) -> str:
        # One input tuple is ONE variable, regardless of the alias used to
        # reference it, so a self-join of t with itself yields x*x = x**2.
        return f"{self.name}.{tuple_id}"


@dataclass
class AnnotatedRelation:
    attrs: tuple[Attr, ...]
    rows: dict[RowKey, Poly]

    @property
    def labels(self) -> tuple[str, ...]:
        return tuple(a.label for a in self.attrs)

    def as_dict(self, key: RowKey) -> dict[str, Any]:
        # Use the bare column name when unique; qualify shared names so joined
        # relations with a common column do not overwrite each other.
        names = [a.name for a in self.attrs]
        counts = {name: names.count(name) for name in set(names)}
        return {
            (attr.name if counts[attr.name] == 1 else attr.label): value
            for attr, value in zip(self.attrs, key)
        }


def base_relation(leaf: LeafRelation) -> AnnotatedRelation:
    """Annotate a leaf: every tuple t contributes the single variable x_t."""
    attrs = tuple(Attr(binding=leaf.binding, name=col) for col in leaf.columns)
    rows: dict[RowKey, Poly] = {}
    for tuple_id, data in leaf.tuples:
        key = tuple(data[col] for col in leaf.columns)
        poly = Poly.var(leaf.variable(tuple_id))
        rows[key] = poly if key not in rows else rows[key] + poly
    return AnnotatedRelation(attrs=attrs, rows=rows)


# --------------------------------------------------------------------------- #
# Column resolution
# --------------------------------------------------------------------------- #
def resolve_attr(attrs: tuple[Attr, ...], reference: str) -> Attr:
    by_label = {a.label: a for a in attrs}
    if reference in by_label:
        return by_label[reference]
    bare_matches = [a for a in attrs if a.name == reference]
    if not bare_matches:
        raise PlanError(
            f"unknown column {reference!r}",
            details={"column": reference, "available": [a.label for a in attrs]},
        )
    if len(bare_matches) > 1:
        raise PlanError(
            f"column {reference!r} is ambiguous; qualify it",
            details={
                "column": reference,
                "candidates": [a.label for a in bare_matches],
            },
        )
    return bare_matches[0]


def _value_for(attrs: tuple[Attr, ...], key: RowKey, reference: str) -> Scalar:
    attr = resolve_attr(attrs, reference)
    return key[attrs.index(attr)]


# --------------------------------------------------------------------------- #
# Three-valued, type-disciplined comparison
# --------------------------------------------------------------------------- #
def _family(value: Scalar) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, (int, float)):
        return "number"
    return "string"


def _as_number(value: Scalar) -> Fraction:
    return Fraction(value) if isinstance(value, int) else Fraction(str(value))


def compare(op: str, left: Scalar, right: Scalar) -> str:
    """Return TRUE / FALSE / UNKNOWN under the supported policy.

    * NULL on either side makes every comparison UNKNOWN (SQL 3VL).
    * Equality across type families is FALSE (and ``!=`` TRUE).
    * Ordering across distinct, non-null families is INDETERMINATE: rather than
      guess a cross-type order we raise a typed error.
    """
    if left is None or right is None:
        return UNKNOWN

    left_family, right_family = _family(left), _family(right)
    if op in ("=", "!="):
        if left_family != right_family:
            equal = False
        elif left_family == "number":
            equal = _as_number(left) == _as_number(right)
        else:
            equal = left == right
        if op == "=":
            return TRUE if equal else FALSE
        return FALSE if equal else TRUE

    # Ordering operators.
    if left_family != right_family:
        raise TypeRuleError(
            f"cannot order values of different types: {left!r} {op} {right!r}",
            details={"left_family": left_family, "right_family": right_family, "op": op},
        )
    if left_family == "number":
        a, b = _as_number(left), _as_number(right)
    elif left_family == "bool":
        raise TypeRuleError(
            f"ordering booleans is not supported: {left!r} {op} {right!r}",
            details={"op": op},
        )
    else:
        a, b = left, right
    result = {
        "<": a < b,
        "<=": a <= b,
        ">": a > b,
        ">=": a >= b,
    }[op]
    return TRUE if result else FALSE


def _evaluate_conjunction(
    predicates: tuple[Comparison, ...], attrs: tuple[Attr, ...], key: RowKey
) -> str:
    """AND over predicates in Kleene three-valued logic."""
    result = TRUE
    for pred in predicates:
        left = _value_for(attrs, key, pred.left)
        if pred.is_column_predicate:
            right = _value_for(attrs, key, pred.right)  # type: ignore[arg-type]
        else:
            right = pred.literal
        truth = compare(pred.op, left, right)
        if truth is FALSE:
            return FALSE
        if truth is UNKNOWN:
            result = UNKNOWN
    return result


# --------------------------------------------------------------------------- #
# Operators
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class RowJudgement:
    """Diagnostic detail for a row considered by selection/join."""

    record_id: str
    truth: str
    columns: tuple[str, ...]
    key: RowKey


def select(
    relation: AnnotatedRelation, predicates: tuple[Comparison, ...]
) -> tuple[AnnotatedRelation, list[RowJudgement]]:
    kept: dict[RowKey, Poly] = {}
    judgements: list[RowJudgement] = []
    for key, poly in relation.rows.items():
        truth = _evaluate_conjunction(predicates, relation.attrs, key)
        record_id = _record_id(relation.attrs, key)
        judgements.append(RowJudgement(record_id, truth, relation.labels, key))
        if truth is TRUE:
            kept[key] = poly
    return AnnotatedRelation(attrs=relation.attrs, rows=kept), judgements


def project(
    relation: AnnotatedRelation, columns: tuple[str, ...]
) -> AnnotatedRelation:
    chosen = tuple(resolve_attr(relation.attrs, ref) for ref in columns)
    out_names = [a.name for a in chosen]
    if len(set(out_names)) != len(out_names):
        raise PlanError(
            "projection produces duplicate output column names",
            details={"columns": out_names},
        )
    indices = [relation.attrs.index(a) for a in chosen]
    out_attrs = tuple(Attr(binding=a.name, name=a.name) for a in chosen)
    rows: dict[RowKey, Poly] = {}
    for key, poly in relation.rows.items():
        new_key = tuple(key[i] for i in indices)
        rows[new_key] = poly if new_key not in rows else rows[new_key] + poly
    return AnnotatedRelation(attrs=out_attrs, rows=rows)


def join(
    left: AnnotatedRelation,
    right: AnnotatedRelation,
    predicates: tuple[Comparison, ...],
) -> tuple[AnnotatedRelation, list[RowJudgement]]:
    overlap = set(left.labels) & set(right.labels)
    if overlap:
        raise PlanError(
            "join inputs share column bindings; alias one side (self-joins need aliases)",
            details={"shared": sorted(overlap)},
        )
    combined_attrs = left.attrs + right.attrs
    rows: dict[RowKey, Poly] = {}
    judgements: list[RowJudgement] = []
    for left_key, left_poly in left.rows.items():
        for right_key, right_poly in right.rows.items():
            combined_key = left_key + right_key
            truth = _evaluate_conjunction(predicates, combined_attrs, combined_key)
            judgements.append(RowJudgement(_join_record_id(left_key, right_key), truth, combined_attrs_labels(combined_attrs), combined_key))
            if truth is TRUE:
                poly = left_poly * right_poly
                rows[combined_key] = (
                    poly if combined_key not in rows else rows[combined_key] + poly
                )
    return AnnotatedRelation(attrs=combined_attrs, rows=rows), judgements


def union(left: AnnotatedRelation, right: AnnotatedRelation) -> AnnotatedRelation:
    """Bag union. Compatibility is positional: equal arity; left schema wins."""
    if len(left.attrs) != len(right.attrs):
        raise PlanError(
            "union branches must have the same arity (column count)",
            details={"left_arity": len(left.attrs), "right_arity": len(right.attrs)},
        )
    rows: dict[RowKey, Poly] = {key: poly for key, poly in left.rows.items()}
    for key, poly in right.rows.items():
        rows[key] = poly if key not in rows else rows[key] + poly
    return AnnotatedRelation(attrs=left.attrs, rows=rows)


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def combined_attrs_labels(attrs: tuple[Attr, ...]) -> tuple[str, ...]:
    return tuple(a.label for a in attrs)


def _record_id(attrs: tuple[Attr, ...], key: RowKey) -> str:
    # Stable synthetic record id from the fully-qualified tuple value.
    return "|".join(f"{attrs[i].label}={_render(key[i])}" for i in range(len(key)))


def _join_record_id(left_key: RowKey, right_key: RowKey) -> str:
    return f"L[{_render_key(left_key)}]⋈R[{_render_key(right_key)}]"


def _render_key(key: RowKey) -> str:
    return ",".join(_render(v) for v in key)


def _render(value: Scalar) -> str:
    return "NULL" if value is None else str(value)


def multiplicity(poly: Poly) -> int:
    """Count derivations: substitute 1 for every input variable."""
    return int(poly.evaluate({name: 1 for name in poly.variables()}))


def sorted_rows(relation: AnnotatedRelation) -> list[tuple[RowKey, Poly]]:
    """Deterministic output order: by column values with NULL sorted first."""
    return sorted(relation.rows.items(), key=lambda kv: _sort_key(relation.attrs, kv[0]))


def _sort_key(attrs: tuple[Attr, ...], key: RowKey) -> tuple:
    out: list[tuple[int, Any]] = []
    for value in key:
        if value is None:
            out.append((0, 0))
        elif isinstance(value, bool):
            out.append((1, int(value)))
        elif isinstance(value, (int, float)):
            out.append((1, float(value)))
        else:
            out.append((2, str(value)))
    return tuple(out)
