"""Executor tests: concrete numerical results against independent NumPy
reference functions, dynamic-shape replanning, and the distinct failure
categories (input_error / state_conflict / resource_exhausted /
computation_failed / replanning_required)."""
from __future__ import annotations

import numpy as np
import pytest


from tenmem.errors import (
    ComputationError,
    GraphValidationError,
    ReplanningRequiredError,
    ResourceExhaustedError,
    StateConflictError,
)
from tenmem.executor import Session, execute, execute_no_reuse
from tenmem.fixtures import (
    alias_feeds,
    alias_reference,
    build_alias,
    build_diamond,
    build_long_lived,
    build_parallel_branches,
    build_shape_mutate,
    diamond_feeds,
    diamond_reference,
    long_lived_feeds,
    long_lived_reference,
    parallel_feeds,
    parallel_reference,
    shape_mutate_feeds,
    shape_mutate_reference,
    workspace_feeds,
    workspace_reference,
    build_workspace_matmul,
)
from tenmem.planner.memory import plan_memory
from tenmem.service import infer_concrete_graph

pytestmark = pytest.mark.unit

ALIGN = 64


# -------------------------------------------------------------------------- #
# Numerical correctness against independent reference expressions
# -------------------------------------------------------------------------- #

def test_diamond_matches_independent_reference() -> None:
    g = build_diamond(8)
    plan = plan_memory(g, alignment=ALIGN)
    feeds = diamond_feeds(8)
    session = execute(g, plan, feeds)
    expected = diamond_reference(feeds["x"])
    np.testing.assert_allclose(session.output("y"), expected, rtol=1e-6, atol=1e-6)


def test_no_reuse_executor_matches_independent_reference() -> None:
    g = build_diamond(8)
    feeds = diamond_feeds(8)
    outputs, trace = execute_no_reuse(g, feeds, alignment=ALIGN)
    np.testing.assert_allclose(outputs["y"], diamond_reference(feeds["x"]))
    assert trace.mode == "no_reuse"
    assert trace.status == "ok"


def test_reuse_and_no_reuse_agree_everywhere() -> None:
    g = build_diamond(8)
    feeds = diamond_feeds(8)
    plan = plan_memory(g, alignment=ALIGN)
    reused = execute(g, plan, feeds).output("y")
    fresh, _ = execute_no_reuse(g, feeds, alignment=ALIGN)
    np.testing.assert_array_equal(reused, fresh["y"])  # bit-exact: same kernels/order


def test_long_lived_outputs_match_reference() -> None:
    g = build_long_lived(16, 4)
    feeds = long_lived_feeds(16)
    session = execute(g, plan_memory(g, alignment=ALIGN), feeds)
    early, final = long_lived_reference(feeds["x"])
    np.testing.assert_allclose(session.output("early"), early)
    np.testing.assert_allclose(session.output("final"), final)


def test_parallel_branches_match_reference_sequential_and_threaded() -> None:
    g, feeds = build_parallel_branches(12, 3), parallel_feeds(12)
    expected = parallel_reference(feeds["x"])
    seq = execute(g, plan_memory(g), feeds, parallel=False)
    par = execute(g, plan_memory(g), feeds, parallel=True)
    np.testing.assert_allclose(seq.output("m2"), expected)
    np.testing.assert_allclose(par.output("m2"), expected)
    assert any(w.parallel for w in par.trace.waves)


def test_alias_chain_matches_reference_and_shares_storage() -> None:
    g, feeds = build_alias(8), alias_feeds(8)
    session = execute(g, plan_memory(g), feeds)
    np.testing.assert_allclose(session.output("c"), alias_reference(feeds["x"]))
    # a and x must literally start at the same backing memory address.
    addr_a = session._arrays["a"].__array_interface__["data"][0]
    addr_x = session._arrays["x"].__array_interface__["data"][0]
    assert addr_a == addr_x


def test_workspace_matmul_matches_reference() -> None:
    g = build_workspace_matmul(4, 5, 6)
    feeds = workspace_feeds(4, 5, 6)
    session = execute(g, plan_memory(g), feeds)
    np.testing.assert_allclose(
        session.output("sm"), workspace_reference(feeds["a"], feeds["b"]), rtol=1e-6
    )


# -------------------------------------------------------------------------- #
# Dynamic shape: in-bound success, over-bound replan, impossible run failure
# -------------------------------------------------------------------------- #

def test_dynamic_shape_within_bound_runs_without_replan() -> None:
    g = build_shape_mutate(capacity=64, bound_n=32)
    feeds = shape_mutate_feeds(10, 64)
    session = execute(g, plan_memory(g), feeds)
    assert session.output("act").shape == (10,)
    np.testing.assert_allclose(session.output("act"), shape_mutate_reference(feeds["data"], 10))


def test_dynamic_shape_over_bound_raises_replanning_required_not_oob() -> None:
    g = build_shape_mutate(capacity=64, bound_n=32)
    feeds = shape_mutate_feeds(48, 64)
    plan = plan_memory(g)
    with pytest.raises(ReplanningRequiredError) as exc:
        execute(g, plan, feeds)
    err = exc.value
    assert err.category == "replanning_required"
    assert err.details["tensor"] == "dyn"
    assert err.details["actual_shape"] == [48]
    assert err.details["bound_hi"] == [32]


def test_replanned_graph_serves_larger_shape_correctly() -> None:
    g = build_shape_mutate(capacity=64, bound_n=32)
    feeds = shape_mutate_feeds(48, 64)
    concrete = infer_concrete_graph(g, feeds)
    session = execute(concrete, plan_memory(concrete), feeds)
    np.testing.assert_allclose(session.output("act"), shape_mutate_reference(feeds["data"], 48))


def test_shape_beyond_source_capacity_is_computation_failure_after_replan() -> None:
    # Even after replanning, tiling 70 elements out of a 64-element source
    # cannot succeed: the kernel must fail as computation_failed, not corrupt.
    g = build_shape_mutate(capacity=64, bound_n=32)
    feeds = shape_mutate_feeds(70, 64)
    concrete = infer_concrete_graph(g, feeds)
    with pytest.raises(ComputationError) as exc:
        execute(concrete, plan_memory(concrete), feeds)
    assert exc.value.category == "computation_failed"
    assert exc.value.details["op"] == "dynamic_tile"


def test_feed_shape_over_declared_bound_is_replanning_required() -> None:
    g = build_shape_mutate(capacity=64, bound_n=32)
    # The data feed itself arrives with more elements than its spec.
    feeds = {"data": np.zeros(80, dtype=np.float32), "length": np.array(80, dtype=np.int64)}
    with pytest.raises(ReplanningRequiredError) as exc:
        execute(g, plan_memory(g), feeds)
    assert exc.value.category == "replanning_required"
    assert exc.value.details["tensor"] == "data"


# -------------------------------------------------------------------------- #
# Input errors
# -------------------------------------------------------------------------- #

def test_missing_feed_is_input_error() -> None:
    g = build_diamond(8)
    with pytest.raises(GraphValidationError) as exc:
        execute(g, plan_memory(g), {})
    assert exc.value.category == "input_error"
    assert "x" in exc.value.details["missing"]


def test_extra_feed_is_input_error() -> None:
    g = build_diamond(8)
    feeds = diamond_feeds(8)
    feeds["bogus"] = np.ones(8, dtype=np.float32)
    with pytest.raises(GraphValidationError) as exc:
        execute(g, plan_memory(g), feeds)
    assert exc.value.category == "input_error"
    assert exc.value.details["extra"] == ["bogus"]


def test_wrong_dtype_feed_is_input_error() -> None:
    g = build_diamond(8)
    feeds = {"x": np.zeros(8, dtype=np.float64)}
    with pytest.raises(GraphValidationError) as exc:
        execute(g, plan_memory(g), feeds)
    assert exc.value.category == "input_error"
    assert "dtype" in exc.value.message


# -------------------------------------------------------------------------- #
# Budget / resource exhaustion at plan time
# -------------------------------------------------------------------------- #

def test_tight_budget_fails_before_execution() -> None:
    g = build_diamond(8)
    with pytest.raises(ResourceExhaustedError) as exc:
        plan_memory(g, max_bytes=10)
    assert exc.value.category == "resource_exhausted"
    assert exc.value.details["max_bytes"] == 10


# -------------------------------------------------------------------------- #
# Pinned output lifecycle / state conflicts
# -------------------------------------------------------------------------- #

def test_output_stays_live_until_release() -> None:
    g = build_long_lived(16, 4)
    plan = plan_memory(g)
    session = execute(g, plan, long_lived_feeds(16))
    assert session.pinned_bytes() > 0
    early_bid = plan.buffer_id_for("early")
    # The output view starts inside the planned backing buffer.
    addr_out = session.output("early").__array_interface__["data"][0]
    addr_buf = _bytearray_address(session._backing[early_bid])
    assert addr_out == addr_buf
    session.release_output("early")
    with pytest.raises(StateConflictError, match="released"):
        session.output("early")


def _bytearray_address(buf) -> int:
    import ctypes

    return ctypes.addressof((ctypes.c_ubyte * len(buf)).from_buffer(buf))


def test_rerun_while_outputs_held_is_state_conflict() -> None:
    g = build_diamond(8)
    session = Session(g, plan_memory(g))
    session.run(diamond_feeds(8))
    with pytest.raises(StateConflictError) as exc:
        session.run(diamond_feeds(8))
    assert exc.value.category == "state_conflict"
    assert exc.value.details["held_outputs"] == ["y"]


def test_rerun_after_release_succeeds() -> None:
    g = build_diamond(8)
    session = Session(g, plan_memory(g))
    session.run(diamond_feeds(8))
    session.release_output("y")
    session.run(diamond_feeds(8))  # no exception
    np.testing.assert_allclose(session.output("y"), diamond_reference(diamond_feeds(8)["x"]))


def test_release_unknown_or_double_is_state_conflict() -> None:
    g = build_diamond(8)
    session = execute(g, plan_memory(g), diamond_feeds(8))
    with pytest.raises(StateConflictError, match="not a graph output"):
        session.release_output("r")
    session.release_output("y")
    with pytest.raises(StateConflictError, match="already released"):
        session.release_output("y")


def test_release_before_execution_is_state_conflict() -> None:
    g = build_diamond(8)
    session = Session(g, plan_memory(g))
    with pytest.raises(StateConflictError, match="before execution"):
        session.release_output("y")
