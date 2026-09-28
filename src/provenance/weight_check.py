"""Independent numeric-weight cross-check.

This is a *second implementation* of the query semantics that never touches
:class:`~provenance.polynomial.Poly`. It propagates plain rational numbers
(one numeric weight per input tuple) through the same operator shape:

* a base tuple carries its injected weight,
* selection is the identity on the weight,
* projection **sums** weights of rows that collapse together,
* join **multiplies** the two sides and sums over pairings,
* union **adds** the branches.

For every output row the independently propagated number must equal the
symbolic polynomial evaluated at the same weights. That equality is the
acceptance test requested in the brief: inject numeric weights and verify the
result equals expression evaluation. Because the two paths share no arithmetic
code, agreement is real evidence rather than self-corroboration.
"""
from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction
from typing import Any

from . import engine
from .errors import InputVersionError, WeightError
from .evidence_store import EvidenceStore
from .rule_language import (
    Join,
    Node,
    Project,
    RelationRef,
    Select,
    Union,
)
from .planner import _children, _collect_relation_refs  # reuse tree walk only

# Numeric annotated relation: same attribute metadata, Fraction values.
NumRow = tuple[engine.Scalar, ...]


def coerce_weights(raw: dict[str, Any]) -> dict[str, Fraction]:
    """Validate an injected ``{variable: number}`` map at the boundary."""
    if not isinstance(raw, dict):
        raise WeightError("'weights' must map provenance variables to numbers")
    weights: dict[str, Fraction] = {}
    for key, value in raw.items():
        try:
            if isinstance(value, bool):
                raise ValueError
            weights[str(key)] = Fraction(str(value))
        except (ValueError, TypeError) as exc:
            raise WeightError(
                f"weight for {key!r} is not a finite number: {value!r}",
                details={"variable": str(key)},
            ) from exc
    return weights


@dataclass
class NumRelation:
    attrs: tuple[engine.Attr, ...]
    rows: dict[NumRow, Fraction]


def evaluate_numeric(
    plan: Node,
    store: EvidenceStore,
    weights: dict[str, Fraction],
    version: str,
) -> NumRelation:
    """Evaluate ``plan`` to output-tuple -> numeric score (Fraction)."""
    return _eval(plan, store, weights, version)


def _eval(node: Node, store: EvidenceStore, weights: dict[str, Fraction], version: str) -> NumRelation:
    if isinstance(node, RelationRef):
        snapshot = store.require_relation(version, node.name)
        attrs = tuple(engine.Attr(binding=node.binding, name=c) for c in snapshot.columns)
        rows: dict[NumRow, Fraction] = {}
        for t in snapshot.rows:
            var = f"{node.name}.{t.tuple_id}"
            if var not in weights:
                raise WeightError(
                    f"missing weight for input tuple {var!r}", details={"variable": var}
                )
            key = tuple(t.data[c] for c in snapshot.columns)
            rows[key] = rows.get(key, Fraction(0)) + weights[var]
        return NumRelation(attrs=attrs, rows=rows)

    if isinstance(node, Select):
        child = _eval(node.child, store, weights, version)
        kept: dict[NumRow, Fraction] = {}
        for key, score in child.rows.items():
            truth = engine._evaluate_conjunction(node.predicates, child.attrs, key)
            if truth == engine.TRUE:
                kept[key] = score
        return NumRelation(attrs=child.attrs, rows=kept)

    if isinstance(node, Project):
        child = _eval(node.child, store, weights, version)
        chosen = tuple(engine.resolve_attr(child.attrs, ref) for ref in node.columns)
        indices = [child.attrs.index(a) for a in chosen]
        out_attrs = tuple(engine.Attr(binding=a.name, name=a.name) for a in chosen)
        projected: dict[NumRow, Fraction] = {}
        for key, score in child.rows.items():
            new_key = tuple(key[i] for i in indices)
            projected[new_key] = projected.get(new_key, Fraction(0)) + score
        return NumRelation(attrs=out_attrs, rows=projected)

    if isinstance(node, Join):
        left = _eval(node.left, store, weights, version)
        right = _eval(node.right, store, weights, version)
        attrs = left.attrs + right.attrs
        joined: dict[NumRow, Fraction] = {}
        for lk, ls in left.rows.items():
            for rk, rs in right.rows.items():
                key = lk + rk
                truth = engine._evaluate_conjunction(node.predicates, attrs, key)
                if truth == engine.TRUE:
                    joined[key] = joined.get(key, Fraction(0)) + ls * rs
        return NumRelation(attrs=attrs, rows=joined)

    if isinstance(node, Union):
        left = _eval(node.left, store, weights, version)
        right = _eval(node.right, store, weights, version)
        merged: dict[NumRow, Fraction] = dict(left.rows)
        for key, score in right.rows.items():
            merged[key] = merged.get(key, Fraction(0)) + score
        return NumRelation(attrs=left.attrs, rows=merged)

    raise InputVersionError(f"unsupported node {node!r}")


@dataclass(frozen=True)
class WeightMismatch:
    output: dict[str, Any]
    symbolic: Fraction
    numeric: Fraction | None


def cross_check(
    result_rows,
    numeric: NumRelation,
    weights: dict[str, Fraction],
) -> list[WeightMismatch]:
    """Compare symbolic evaluation against the independent numeric propagation.

    For each symbolic output row, evaluate its polynomial at ``weights`` and
    look up the same output tuple in the independently computed numeric result.
    Returns the mismatches (an empty list means the two derivations agree).
    Also flags any numeric output row that has no symbolic counterpart.
    """
    numeric_by_key: dict[NumRow, Fraction] = dict(numeric.rows)
    mismatches: list[WeightMismatch] = []
    seen: set[NumRow] = set()

    def lookup(row_values: dict[str, Any], attr: engine.Attr) -> Any:
        return row_values.get(attr.label, row_values.get(attr.name))

    for row in result_rows:
        key = tuple(lookup(row.values, attr) for attr in numeric.attrs)
        seen.add(key)
        symbolic = row.poly.evaluate(weights)
        numeric_value = numeric_by_key.get(key)
        if numeric_value is None or symbolic != numeric_value:
            mismatches.append(
                WeightMismatch(output=dict(row.values), symbolic=symbolic, numeric=numeric_value)
            )
    for key, score in numeric_by_key.items():
        if key not in seen:
            mismatches.append(
                WeightMismatch(
                    output={attr.label: value for attr, value in zip(numeric.attrs, key)},
                    symbolic=Fraction(0),
                    numeric=score,
                )
            )
    return mismatches
