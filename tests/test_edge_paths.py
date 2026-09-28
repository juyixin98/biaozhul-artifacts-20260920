"""Edge-path tests: constants, no-reuse failure wrapping, unknown sessions,
log helpers and custom graph submission through the HTTP boundary."""
from __future__ import annotations

import numpy as np
import pytest


from tenmem.errors import ComputationError, GraphValidationError, StateConflictError
from tenmem.executor import Session, execute_no_reuse
from tenmem.graph import Graph, Node
from tenmem.planner.memory import plan_memory
from tenmem.tensor import TensorSpec

pytestmark = pytest.mark.unit


def _const_graph() -> Graph:
    x = TensorSpec("x", (4,), "float32")
    c = TensorSpec("c", (4,), "float32")
    return Graph(
        "with-const",
        (x,),
        (Node("addc", "add", ("x", "c"), (TensorSpec("y", (4,), "float32"),)),),
        ("y",),
        constants=(c,),
    )


def test_execution_with_constant() -> None:
    g = _const_graph()
    plan = plan_memory(g)
    x = np.array([1.0, 2.0, 3.0, 4.0], dtype=np.float32)
    c = np.array([10.0, 20.0, 30.0, 40.0], dtype=np.float32)
    session = Session(g, plan).run({"x": x}, {"c": c})
    np.testing.assert_array_equal(session.output("y"), x + c)


def test_missing_constant_is_input_error() -> None:
    g = _const_graph()
    with pytest.raises(GraphValidationError, match="missing constant"):
        Session(g, plan_memory(g)).run({"x": np.ones(4, np.float32)}, {})


def test_unknown_constant_is_input_error() -> None:
    g = _const_graph()
    with pytest.raises(GraphValidationError, match="unknown constant"):
        Session(g, plan_memory(g)).run(
            {"x": np.ones(4, np.float32)},
            {"c": np.ones(4, np.float32), "z": np.ones(4, np.float32)},
        )


def test_no_reuse_wraps_kernel_failure_as_computation_error() -> None:
    # n beyond source capacity fails the tile kernel even with fresh buffers.
    from tenmem.fixtures import build_shape_mutate, shape_mutate_feeds
    from tenmem.service import infer_concrete_graph

    g = build_shape_mutate(capacity=64, bound_n=32)
    feeds = shape_mutate_feeds(70, 64)
    concrete = infer_concrete_graph(g, feeds)
    with pytest.raises(ComputationError) as exc:
        execute_no_reuse(concrete, feeds)
    assert exc.value.category == "computation_failed"
    assert exc.value.details["op"] == "dynamic_tile"


def test_engine_unknown_session_and_release(engine) -> None:
    with pytest.raises(StateConflictError, match="unknown run"):
        engine.session("nope")
    with pytest.raises(StateConflictError, match="unknown run"):
        engine.release("nope", "y")


def test_run_logger_all_runs_and_latest(tmp_path) -> None:
    from tenmem.runlog import RunLogger

    logger = RunLogger(tmp_path / "logs")
    logger.event("run-a", "run_start", graph="g")
    logger.event("run-b", "run_start", graph="g")
    assert {r["run_id"] for r in logger.all_runs()} == {"run-a", "run-b"}
    assert logger.latest_run_id() == "run-b"
    assert [r["kind"] for r in logger.replay("run-a")] == ["run_start"]
    assert logger.replay("missing") == []
