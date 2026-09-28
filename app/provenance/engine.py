"""Provenance-aware positive-relational evaluation engine.

Intermediate relations are *K-relations*: a map from an answer tuple to the
``Polynomial`` that witnesses it.  The relational operators are exactly the
semiring homomorphisms of the N[X] semiring:

* **select**  : keep qualifying tuples with their polynomial unchanged.
* **join**    : for every pair satisfying the equi-join condition, emit the
  concatenated tuple with the *product* of the two polynomials.
* **project** : group tuples that become equal after dropping columns and
  *sum* their polynomials.
* **union**   : group equal tuples from both bag inputs and *sum*.

The tuple order emitted by a join is fixed by the input row ordinals, so query
results are deterministic for a given input version.
"""
from __future__ import annotations

import operator
from dataclasses import dataclass
from typing import Mapping

from . import language as lang
from .expressions import Polynomial
from .store import RelationData

_COMPARATORS = {
    "=": operator.eq,
    "!=": operator.ne,
    "<": operator.lt,
    "<=": operator.le,
    ">": operator.gt,
    ">=": operator.ge,
}


@dataclass(frozen=True)
class AnswerRow:
    values: tuple
    provenance: Polynomial


@dataclass(frozen=True)
class QueryResult:
    labels: tuple[str, ...]
    rows: tuple[AnswerRow, ...]


# A K-relation maps a value tuple (aligned with a label tuple) to a polynomial.
_KRelation = dict[tuple, Polynomial]


def evaluate(
    node: lang.Node, relations: Mapping[str, RelationData]
) -> QueryResult:
    krel, labels = _evaluate_node(node, relations)
    rows = tuple(AnswerRow(values=values, provenance=poly)
                 for values, poly in krel.items())
    return QueryResult(labels=labels, rows=rows)


def _evaluate_node(
    node: lang.Node, relations: Mapping[str, RelationData]
) -> tuple[_KRelation, tuple[str, ...]]:
    if node.kind == "relation":
        return _scan(node, relations), node.output_labels
    if node.kind == "select":
        return _select(node, relations), node.output_labels
    if node.kind == "project":
        return _project(node, relations), node.output_labels
    if node.kind == "join":
        return _join(node, relations), node.output_labels
    if node.kind == "union":
        return _union(node, relations), node.output_labels
    raise AssertionError(f"unhandled node kind {node.kind!r}")  # pragma: no cover


def _scan(node: lang.RelationScan, relations: Mapping[str, RelationData]) -> _KRelation:
    if node.relation not in relations:
        raise KeyError(f"relation {node.relation!r} was not provided to the engine")
    data = relations[node.relation]
    result: _KRelation = {}
    for row in data.rows:
        if len(row.values) != len(data.columns):
            raise ValueError(
                f"row {row.row_id!r} arity does not match declared columns"
            )
        witness = f"{node.relation}.{row.row_id}"
        # Two stored rows could be value-equal (duplicates): they are distinct
        # witnesses, so their contributions add.
        result[row.values] = result.get(row.values, Polynomial.zero()) + (
            Polynomial.var(witness)
        )
    return result


def _select(node: lang.Select, relations: Mapping[str, RelationData]) -> _KRelation:
    child, labels = _evaluate_node(node.input, relations)
    column_index = labels.index(node.predicate.label)
    result: _KRelation = {}
    for values, poly in child.items():
        if _predicate_holds(node.predicate, values[column_index]):
            result[values] = poly
    return result


def _predicate_holds(predicate: lang.Predicate, cell) -> bool:
    op = predicate.op
    if op == "is_null":
        return cell is None
    if op == "is_not_null":
        return cell is not None
    # NULL never satisfies a comparison (SQL three-valued logic -> UNKNOWN).
    if cell is None or predicate.value is None:
        return False
    try:
        return bool(_COMPARATORS[op](cell, predicate.value))
    except TypeError:
        # Incomparable types (e.g. string vs number) never satisfy the test.
        return False


def _project(node: lang.Project, relations: Mapping[str, RelationData]) -> _KRelation:
    child, labels = _evaluate_node(node.input, relations)
    indices = [labels.index(col) for col in node.columns]
    result: _KRelation = {}
    for values, poly in child.items():
        projected = tuple(values[i] for i in indices)
        result[projected] = result.get(projected, Polynomial.zero()) + poly
    return result


def _join(node: lang.Join, relations: Mapping[str, RelationData]) -> _KRelation:
    left, left_labels = _evaluate_node(node.left, relations)
    right, right_labels = _evaluate_node(node.right, relations)
    pair_indices = [
        (left_labels.index(l_label), right_labels.index(r_label))
        for l_label, r_label in node.on
    ]
    result: _KRelation = {}
    for l_values, l_poly in left.items():
        for r_values, r_poly in right.items():
            if _join_keys_match(l_values, r_values, pair_indices):
                merged = l_values + r_values
                result[merged] = result.get(merged, Polynomial.zero()) + (
                    l_poly * r_poly
                )
    return result


def _join_keys_match(
    l_values: tuple, r_values: tuple, pair_indices: list[tuple[int, int]]
) -> bool:
    for l_index, r_index in pair_indices:
        left, right = l_values[l_index], r_values[r_index]
        # NULL never joins, not even to another NULL.
        if left is None or right is None:
            return False
        if left != right:
            return False
    return True


def _union(node: lang.Union, relations: Mapping[str, RelationData]) -> _KRelation:
    left, _ = _evaluate_node(node.left, relations)
    right, _ = _evaluate_node(node.right, relations)
    # Inputs are set-compatible (validated by the parser); values align
    # positionally, so equal tuples from the two bags are summed together.
    result: _KRelation = dict(left)
    for values, poly in right.items():
        result[values] = result.get(values, Polynomial.zero()) + poly
    return result
