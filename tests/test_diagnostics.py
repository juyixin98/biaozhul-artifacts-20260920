"""Additional diagnostics tests: run-log file replay, wall-clock budget,
misc ops and state lifecycle."""

from __future__ import annotations

import time

import numpy as np
import pytest

from hvpsvc import autodiff as ad
from hvpsvc.errors import ResourceExhaustedError
from hvpsvc.graph import Budget
from hvpsvc.state import StateStore

from . import fixtures as fx


def _state(spec, point):
    st = StateStore().create(spec)
    st.set_point(point)
    return st


def test_run_logger_scans_file_when_not_in_memory(tmp_path):
    from hvpsvc.runs import RunLogger

    path = tmp_path / "runs.jsonl"
    logger = RunLogger(path, keep=1)
    rid_a = logger.new_run_id()
    rid_b = logger.new_run_id()
    logger.record(rid_a, "hvp", status="ok",
                  result_summary={"hvp_norm": 1.0})
    logger.record(rid_b, "gradient", status="ok")
    # keep=1 evicted rid_a from memory; file scan still replays it
    replay = logger.get(rid_a)
    assert replay is not None
    assert replay["result_summary"]["hvp_norm"] == 1.0
    assert logger.get("run-does-not-exist") is None


def test_wall_clock_budget_is_resource_exhausted():
    budget = Budget(max_evals=10_000_000,
                    deadline_monotonic=time.monotonic() - 1.0)
    with pytest.raises(ResourceExhaustedError) as ei:
        budget.tick_eval()
    assert ei.value.detail["limit"] == "wall_clock"


def test_sign_zero_kink_and_sign_off_kink():
    from hvpsvc.errors import NonSmoothError
    from hvpsvc.graph import NonsmoothConfig

    spec = {
        "variables": [{"name": "x", "shape": []}],
        "expression": {"nodes": [{"id": "s", "op": "sign", "args": ["x"]}],
                       "output": "s"},
    }
    st = _state(spec, {"x": 0.0})
    with pytest.raises(NonSmoothError):
        st.gradient_graph(st.new_budget())
    st2 = StateStore().create(
        spec, nonsmooth=NonsmoothConfig(policy="subgradient",
                                        subgradient=0.75))
    st2.set_point({"x": 0.0})
    grad = ad.gradient(st2.gradient_graph(st2.new_budget()),
                       st2.point, st2.layout, st2.new_budget())
    # sign at kink: 2*0.75 - 1 = 0.5
    assert grad[0] == pytest.approx(0.5)
    # off the kink sign is locally constant -> gradient zero
    st3 = _state(spec, {"x": 3.0})
    g3 = ad.gradient(st3.gradient_graph(st3.new_budget()),
                     st3.point, st3.layout, st3.new_budget())
    assert g3[0] == 0.0


def test_min_op_and_neg_cos_rules():
    spec = {
        "variables": [{"name": "x", "shape": [2]}],
        "expression": {
            "nodes": [
                {"id": "x0", "op": "get", "args": ["x"], "index": 0},
                {"id": "x1", "op": "get", "args": ["x"], "index": 1},
                {"id": "m", "op": "min", "args": ["x0", "x1"]},
                {"id": "f", "op": "neg", "args": ["m"]},
            ],
            "output": "f",
        },
    }
    st = _state(spec, {"x": [2.0, 5.0]})  # min = x0, f = -x0 locally
    gg = st.gradient_graph(st.new_budget())
    grad = ad.gradient(gg, st.point, st.layout, st.new_budget())
    np.testing.assert_allclose(grad, [-1.0, 0.0])
    hv = ad.hvp(gg, st.point, np.array([1.0, 1.0]), st.layout,
                st.new_budget(), st.nonsmooth)
    np.testing.assert_allclose(hv, [0.0, 0.0])


def test_state_version_invalidates_gradient_cache():
    st = _state(fx.scalar_square_spec(), {"s": 1.0})
    gg1 = st.gradient_graph(st.new_budget())
    assert gg1.primal_value == pytest.approx(2.0)
    st.set_point({"s": 4.0})
    gg2 = st.gradient_graph(st.new_budget())
    assert gg2 is not gg1
    assert gg2.primal_value == pytest.approx(20.0)


def test_state_lifecycle_delete_and_unknown():
    store = StateStore()
    st = store.create(fx.scalar_square_spec())
    assert len(store) == 1
    store.delete(st.state_id)
    assert len(store) == 0
    from hvpsvc.errors import StateConflictError
    with pytest.raises(StateConflictError):
        store.delete(st.state_id)
