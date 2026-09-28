"""Additional coverage for the numeric path over select/union and store edges."""
from __future__ import annotations

from fractions import Fraction

import pytest

from provenance.errors import SnapshotError
from provenance.rule_language import parse_plan
from provenance.weight_check import coerce_weights, cross_check, evaluate_numeric


def test_numeric_select_project_path(store, planner):
    plan = {
        "op": "project",
        "columns": ["dst"],
        "child": {
            "op": "select",
            "child": {"op": "relation", "name": "edge", "version": "v1"},
            "predicates": [{"op": "=", "left": "src", "literal": "a"}],
        },
    }
    weights = {f"edge.e{i}": Fraction(p) for i, p in enumerate([2, 3, 5, 7, 11, 13], start=1)}
    result = planner.run(plan, request_id="w-select")
    numeric = evaluate_numeric(parse_plan(plan), store, weights, "v1")
    # dst='b' from e1(2)+e3(5)=7 ; the NULL tuple e5 is excluded by selection.
    assert numeric.rows[("b",)] == Fraction(7)
    assert cross_check(result.rows, numeric, weights) == []


def test_numeric_union_path(store, planner):
    plan = {
        "op": "union",
        "left": {"op": "relation", "name": "edge", "version": "v1"},
        "right": {"op": "relation", "name": "redge", "version": "v1"},
    }
    weights = {
        **{f"edge.e{i}": Fraction(1) for i in range(1, 7)},
        "redge.r1": Fraction(4),
        "redge.r2": Fraction(6),
    }
    result = planner.run(plan, request_id="w-union")
    numeric = evaluate_numeric(parse_plan(plan), store, weights, "v1")
    assert numeric.rows[("b", "a")] == Fraction(4)
    assert cross_check(result.rows, numeric, weights) == []


def test_null_literal_comparison_is_unknown_numeric(store, planner):
    plan = {
        "op": "select",
        "child": {"op": "relation", "name": "edge", "version": "v1"},
        "predicates": [{"op": "=", "left": "dst", "literal": None}],
    }
    weights = coerce_weights({f"edge.e{i}": 1 for i in range(1, 7)})
    numeric = evaluate_numeric(parse_plan(plan), store, weights, "v1")
    # dst = NULL is UNKNOWN for every row, including e5 (dst already NULL).
    assert numeric.rows == {}


def test_duplicate_snapshot_version_rejected(store):
    import json
    from pathlib import Path

    from provenance.evidence_store import Snapshot

    raw = json.loads(
        (Path(__file__).resolve().parents[1] / "fixtures" / "graph_v1.json").read_text()
    )
    with pytest.raises(SnapshotError) as exc:
        store.load_snapshot(Snapshot.from_dict(raw))
    assert exc.value.category == "rejected_snapshot"


def test_replace_snapshot_version_succeeds(store):
    import json
    from pathlib import Path

    from provenance.evidence_store import Snapshot

    raw = json.loads(
        (Path(__file__).resolve().parents[1] / "fixtures" / "graph_v1.json").read_text()
    )
    hashes = store.load_snapshot(Snapshot.from_dict(raw), replace=True)
    assert "edge" in hashes
    # Old tuples were replaced, not duplicated.
    assert len(store.require_relation("v1", "edge").rows) == 6


def test_read_back_unknown_returns_none(store):
    assert store.read_back("does-not-exist") is None


def test_invalid_fixture_shapes(store):
    from provenance.evidence_store import Snapshot

    bad_cases = [
        {"version": "vx"},  # no relations
        {"version": "vx", "relations": {"R": {"columns": ["a"], "rows": []}}},  # actually valid
    ]
    # First case must fail; second is a minimal valid snapshot.
    with pytest.raises(SnapshotError):
        Snapshot.from_dict(bad_cases[0])
    snapshot = Snapshot.from_dict(bad_cases[1])
    assert snapshot.relations[0].rows == ()
