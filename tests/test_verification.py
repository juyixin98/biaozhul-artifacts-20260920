"""Tests for the numerical verification boundary and plan invariants."""
from __future__ import annotations

import numpy as np
import pytest


from tenmem.fixtures import build_diamond, diamond_feeds, diamond_reference
from tenmem.planner.memory import plan_memory
from tenmem.executor import execute, execute_no_reuse
from tenmem.verification import assert_plan_invariants, compare_outputs

pytestmark = pytest.mark.unit


def test_compare_outputs_ok() -> None:
    a = {"y": np.arange(4, dtype=np.float32)}
    v = compare_outputs(a, {"y": np.arange(4, dtype=np.float32)})
    assert v.equivalent
    assert v.max_abs_diff == 0.0
    assert v.tensor_verdicts[0].reason == "ok"


def test_compare_outputs_detects_shape_mismatch() -> None:
    v = compare_outputs({"y": np.zeros(3, np.float32)}, {"y": np.zeros(4, np.float32)})
    assert not v.equivalent
    assert v.tensor_verdicts[0].reason == "shape mismatch"


def test_compare_outputs_detects_non_finite() -> None:
    got = {"y": np.array([1.0, np.inf], dtype=np.float32)}
    v = compare_outputs(got, {"y": np.array([1.0, 2.0], dtype=np.float32)})
    assert not v.equivalent
    assert "non-finite" in v.tensor_verdicts[0].reason


def test_compare_outputs_detects_tolerance_breach() -> None:
    got = {"y": np.array([1.0, 2.0, 3.0], dtype=np.float32)}
    v = compare_outputs(got, {"y": np.array([1.0, 2.0, 3.5], dtype=np.float32)})
    assert not v.equivalent
    assert "tolerance" in v.tensor_verdicts[0].reason


def test_compare_outputs_missing_reference() -> None:
    v = compare_outputs({"y": np.zeros(2, np.float32)}, {})
    assert not v.equivalent
    assert v.tensor_verdicts[0].reason == "reference tensor missing"


def test_plan_invariants_hold_for_all_fixture_graphs() -> None:
    from tenmem import fixtures as fx

    graphs_feeds = [
        (build_diamond(8), diamond_feeds(8)),
        (fx.build_long_lived(), fx.long_lived_feeds()),
        (fx.build_parallel_branches(12, 3), fx.parallel_feeds(12)),
        (fx.build_alias(), fx.alias_feeds()),
        (fx.build_workspace_matmul(), fx.workspace_feeds()),
    ]
    for graph, feeds in graphs_feeds:
        plan = plan_memory(graph)
        assert_plan_invariants(plan)  # independent of planner's own checker


def test_triple_agreement_on_diamond() -> None:
    # reuse executor == no-reuse executor == hand-written NumPy reference.
    graph = build_diamond(8)
    feeds = diamond_feeds(8)
    reused = execute(graph, plan_memory(graph), feeds).output("y")
    fresh, _ = execute_no_reuse(graph, feeds)
    hand = diamond_reference(feeds["x"])
    assert compare_outputs({"y": reused}, {"y": fresh["y"]}).equivalent
    assert compare_outputs({"y": reused}, {"y": hand}).equivalent
