"""Planner / orchestration kernel.

It sits between the rule language and the pure engine and owns the decisions
the engine must not know about:

* **single input version** -- every ``relation`` leaf in one plan must pin the
  same version, so the answer and its provenance can only ever come from one
  consistent input snapshot (behavioural contract #4),
* **binding resolution** -- relation names/aliases are resolved against the
  evidence store and checked for unique, non-colliding bindings,
* **schema flow** -- each node's output attributes are derived bottom-up and
  predicates are validated against them,
* **diagnostics** -- why each row was accepted/rejected and why a plan was
  rejected or could not be decided.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Any

from . import engine
from .diagnostics import JsonDiagnostics
from .errors import InputVersionError, PlanError
from .evidence_store import EvidenceStore
from .polynomial import Poly
from .rule_language import (
    Join,
    Node,
    Project,
    RelationRef,
    Select,
    Union,
    parse_plan,
)


@dataclass(frozen=True)
class OutputRow:
    values: dict[str, Any]
    poly: Poly
    multiplicity: int


@dataclass
class QueryResult:
    request_id: str
    version: str
    plan_hash: str
    input_hashes: dict[str, str]
    rows: list[OutputRow] = field(default_factory=list)

    def to_payload(self) -> dict[str, Any]:
        return {
            "request_id": self.request_id,
            "version": self.version,
            "plan_hash": self.plan_hash,
            "input_hashes": self.input_hashes,
            "rows": [
                {
                    "output": row.values,
                    "multiplicity": row.multiplicity,
                    "provenance": row.poly.to_string(),
                    "provenance_terms": row.poly.to_dict(),
                }
                for row in self.rows
            ],
        }


def _plan_hash(plan: Node) -> str:
    structure = _structure(plan)
    import json

    return hashlib.sha256(
        json.dumps(structure, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()[:16]


def _structure(node: Node) -> Any:
    if isinstance(node, RelationRef):
        return {"op": "relation", "name": node.name, "binding": node.binding}
    if isinstance(node, Select):
        return {
            "op": "select",
            "child": _structure(node.child),
            "predicates": [_pred_structure(p) for p in node.predicates],
        }
    if isinstance(node, Project):
        return {"op": "project", "child": _structure(node.child), "columns": list(node.columns)}
    if isinstance(node, Join):
        return {
            "op": "join",
            "left": _structure(node.left),
            "right": _structure(node.right),
            "predicates": [_pred_structure(p) for p in node.predicates],
        }
    if isinstance(node, Union):
        return {"op": "union", "left": _structure(node.left), "right": _structure(node.right)}
    raise PlanError(f"unsupported plan node {node!r}")


def _pred_structure(pred: Any) -> dict[str, Any]:
    if pred.is_column_predicate:
        return {"op": pred.op, "left": pred.left, "right": pred.right}
    return {"op": pred.op, "left": pred.left, "literal": pred.literal}


def _collect_versions(node: Node, found: set[str]) -> None:
    if isinstance(node, RelationRef):
        found.add(node.version)
        return
    for child in _children(node):
        _collect_versions(child, found)


def _children(node: Node) -> tuple[Node, ...]:
    if isinstance(node, (Select, Project)):
        return (node.child,)
    if isinstance(node, (Join, Union)):
        return (node.left, node.right)
    return ()


class Planner:
    def __init__(self, store: EvidenceStore, diagnostics: JsonDiagnostics | None = None):
        self.store = store
        self.diag = diagnostics or JsonDiagnostics()

    def run(self, raw_plan: Any, *, request_id: str) -> QueryResult:
        plan = parse_plan(raw_plan)

        versions: set[str] = set()
        _collect_versions(plan, versions)
        if len(versions) != 1:
            # Same-version answer and provenance is a hard requirement; mixing
            # versions is rejected rather than silently merged.
            raise InputVersionError(
                "a query must read exactly one input version across all relations",
                details={"versions": sorted(versions)},
            )
        version = next(iter(versions))

        bindings = self._check_bindings(plan)
        plan_hash = _plan_hash(plan)
        leaf_refs = _collect_relation_refs(plan)
        input_hashes = {ref.name: self.store.content_hash(version, ref.name) for ref in leaf_refs}

        self.store.start_run(request_id, [version], plan_hash)
        try:
            annotated, leaves = self._execute(plan, request_id, bindings, version)
            rows = [
                OutputRow(
                    values=annotated.as_dict(key),
                    poly=poly,
                    multiplicity=engine.multiplicity(poly),
                )
                for key, poly in engine.sorted_rows(annotated)
            ]
            result = QueryResult(
                request_id=request_id,
                version=version,
                plan_hash=plan_hash,
                input_hashes=input_hashes,
                rows=rows,
            )
            self.store.finish_run(request_id, "completed", _persistable(result))
            self.diag.accepted(
                "query completed",
                {"version": version, "output_rows": len(rows), "plan_hash": plan_hash},
                request_id=request_id,
            )
            return result
        except Exception:
            self.store.finish_run(request_id, "failed", [])
            raise

    # ------------------------------------------------------------------ #
    def _check_bindings(self, plan: Node) -> set[str]:
        refs = _collect_relation_refs(plan)
        bindings = [ref.binding for ref in refs]
        duplicates = {b for b in bindings if bindings.count(b) > 1}
        if duplicates:
            raise PlanError(
                "duplicate relation bindings; give self-joins distinct aliases",
                details={"duplicates": sorted(duplicates)},
            )
        return set(bindings)

    def _execute(
        self,
        node: Node,
        request_id: str,
        bindings: set[str],
        version: str,
    ) -> tuple[engine.AnnotatedRelation, dict[str, engine.LeafRelation]]:
        if isinstance(node, RelationRef):
            snapshot = self.store.require_relation(version, node.name)
            leaf = engine.LeafRelation(
                binding=node.binding,
                name=node.name,
                columns=snapshot.columns,
                tuples=tuple((t.tuple_id, t.data) for t in snapshot.rows),
            )
            self.diag.accepted(
                "relation leaf resolved from pinned version",
                {
                    "relation": node.name,
                    "binding": node.binding,
                    "version": version,
                    "tuple_count": len(leaf.tuples),
                },
                request_id=request_id,
                record_id=node.binding,
            )
            return engine.base_relation(leaf), {node.binding: leaf}

        if isinstance(node, Select):
            child, leaves = self._execute(node.child, request_id, bindings, version)
            self._validate_predicates(node.predicates, child.attrs, allow_literal=True)
            result, judgements = engine.select(child, node.predicates)
            for j in judgements:
                self._emit_row_decision("select", j, request_id)
            return result, leaves

        if isinstance(node, Project):
            child, leaves = self._execute(node.child, request_id, bindings, version)
            result = engine.project(child, node.columns)
            return result, leaves

        if isinstance(node, Join):
            left, left_leaves = self._execute(node.left, request_id, bindings, version)
            right, right_leaves = self._execute(node.right, request_id, bindings, version)
            self._validate_predicates(
                node.predicates, left.attrs + right.attrs, allow_literal=False
            )
            result, judgements = engine.join(left, right, node.predicates)
            for j in judgements:
                self._emit_row_decision("join", j, request_id)
            return result, {**left_leaves, **right_leaves}

        if isinstance(node, Union):
            left, left_leaves = self._execute(node.left, request_id, bindings, version)
            right, right_leaves = self._execute(node.right, request_id, bindings, version)
            result = engine.union(left, right)
            return result, {**left_leaves, **right_leaves}

        raise PlanError(f"unsupported plan node {node!r}")

    # ------------------------------------------------------------------ #
    def _validate_predicates(
        self, predicates: tuple, attrs: tuple[engine.Attr, ...], *, allow_literal: bool
    ) -> None:
        for pred in predicates:
            engine.resolve_attr(attrs, pred.left)
            if pred.is_column_predicate:
                engine.resolve_attr(attrs, pred.right)
            elif not allow_literal:
                raise PlanError("join predicates must compare two columns")

    def _emit_row_decision(self, operator: str, judgement: engine.RowJudgement, request_id: str) -> None:
        state = {
            "operator": operator,
            "columns": list(judgement.columns),
            "truth": judgement.truth,
        }
        if judgement.truth == engine.TRUE:
            self.diag.accepted("row satisfied predicate", state, request_id=request_id, record_id=judgement.record_id)
        elif judgement.truth == engine.UNKNOWN:
            self.diag.indeterminable(
                "row excluded: predicate is UNKNOWN under NULL three-valued logic",
                state,
                request_id=request_id,
                record_id=judgement.record_id,
            )
        else:
            self.diag.rejected(
                "row excluded: predicate false",
                state,
                request_id=request_id,
                record_id=judgement.record_id,
            )


def _collect_relation_refs(node: Node) -> tuple[RelationRef, ...]:
    if isinstance(node, RelationRef):
        return (node,)
    refs: list[RelationRef] = []
    for child in _children(node):
        refs.extend(_collect_relation_refs(child))
    return tuple(refs)


def _persistable(result: QueryResult) -> list[dict[str, Any]]:
    return [
        {
            "output": row.values,
            "multiplicity": row.multiplicity,
            "poly": row.poly.to_dict(),
        }
        for row in result.rows
    ]
