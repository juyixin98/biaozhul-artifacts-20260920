"""训练状态与 RNG 快照测试。"""

from __future__ import annotations

import numpy as np
import pytest

from recomp_scheduler.errors import GraphValidationError, StateConflictError
from recomp_scheduler.state import TrainState


def test_synthetic_state_matches_graph_shapes(chain_graph) -> None:
    state = TrainState.synthetic(chain_graph, seed=1)
    assert state.inputs["x"].shape == (4, 2)
    assert state.params["l1"]["W"].shape == (2, 3)
    assert state.params["l1"]["b"].shape == (3,)
    assert state.grad_outputs["l2"].shape == (4, 1)


def test_synthetic_state_is_deterministic(chain_graph) -> None:
    a = TrainState.synthetic(chain_graph, seed=123)
    b = TrainState.synthetic(chain_graph, seed=123)
    np.testing.assert_array_equal(a.inputs["x"], b.inputs["x"])
    np.testing.assert_array_equal(a.params["l1"]["W"], b.params["l1"]["W"])


def test_missing_input_feed_is_input_error(chain_graph) -> None:
    with pytest.raises(GraphValidationError) as exc:
        TrainState(
            graph=chain_graph,
            inputs={},
            params=TrainState.synthetic(chain_graph).params,
            grad_outputs=TrainState.synthetic(chain_graph).grad_outputs,
        )
    assert exc.value.category == "input_error"
    assert exc.value.details["node"] == "x"


def test_input_shape_mismatch_is_input_error(chain_graph) -> None:
    good = TrainState.synthetic(chain_graph)
    bad_inputs = dict(good.inputs)
    bad_inputs["x"] = np.zeros((4, 9))  # 错误形状
    with pytest.raises(GraphValidationError) as exc:
        TrainState(
            graph=chain_graph,
            inputs=bad_inputs,
            params=good.params,
            grad_outputs=good.grad_outputs,
        )
    assert exc.value.details["expected"] == (4, 2)


def test_missing_param_is_input_error(chain_graph) -> None:
    good = TrainState.synthetic(chain_graph)
    with pytest.raises(GraphValidationError):
        TrainState(
            graph=chain_graph,
            inputs=good.inputs,
            params={},
            grad_outputs=good.grad_outputs,
        )


def test_rng_snapshot_restore_roundtrip(chain_graph) -> None:
    state = TrainState.synthetic(chain_graph, seed=5)
    token = state.rng_snapshot(0)
    before = state.rng.random(3)
    state.restore_rng(token, 0)
    after = state.rng.random(3)
    np.testing.assert_array_equal(before, after)


def test_rng_restore_same_block_twice_is_conflict(chain_graph) -> None:
    state = TrainState.synthetic(chain_graph, seed=5)
    token = state.rng_snapshot(0)
    state.restore_rng(token, 0)
    with pytest.raises(StateConflictError) as exc:
        state.restore_rng(token, 0)
    assert exc.value.category == "state_conflict"


def test_identical_snapshots_for_different_blocks_are_distinct(chain_graph) -> None:
    # 相邻块入口 RNG 状态可能相同（块内无随机算子），但仍按块号独立恢复。
    state = TrainState.synthetic(chain_graph, seed=5)
    t0 = state.rng_snapshot(0)
    t1 = state.rng_snapshot(1)
    state.restore_rng(t0, 0)
    state.restore_rng(t1, 1)  # 不应误报重复
    assert state.snapshot_balance() == {"taken": 2, "restored": 2}


def test_clone_is_independent(chain_graph) -> None:
    state = TrainState.synthetic(chain_graph, seed=5)
    clone = state.clone()
    clone.inputs["x"][0, 0] += 100.0
    assert clone.inputs["x"][0, 0] != state.inputs["x"][0, 0]


def test_input_wrong_dtype_is_input_error(chain_graph) -> None:
    good = TrainState.synthetic(chain_graph)
    bad = dict(good.inputs)
    bad["x"] = good.inputs["x"].astype(np.float32)
    with pytest.raises(GraphValidationError) as exc:
        TrainState(
            graph=chain_graph,
            inputs=bad,
            params=good.params,
            grad_outputs=good.grad_outputs,
        )
    assert exc.value.details["actual"] == "float32"


def test_non_finite_input_is_input_error(chain_graph) -> None:
    good = TrainState.synthetic(chain_graph)
    bad = dict(good.inputs)
    bad["x"] = good.inputs["x"].copy()
    bad["x"][0, 0] = np.inf
    with pytest.raises(GraphValidationError):
        TrainState(
            graph=chain_graph,
            inputs=bad,
            params=good.params,
            grad_outputs=good.grad_outputs,
        )


def test_missing_grad_output_is_input_error(chain_graph) -> None:
    good = TrainState.synthetic(chain_graph)
    with pytest.raises(GraphValidationError) as exc:
        TrainState(
            graph=chain_graph,
            inputs=good.inputs,
            params=good.params,
            grad_outputs={},
        )
    assert exc.value.details["node"] == "l2"


def test_grad_output_shape_mismatch_is_input_error(chain_graph) -> None:
    good = TrainState.synthetic(chain_graph)
    with pytest.raises(GraphValidationError):
        TrainState(
            graph=chain_graph,
            inputs=good.inputs,
            params=good.params,
            grad_outputs={"l2": np.ones((4, 7))},
        )


def test_param_shape_mismatch_is_input_error(chain_graph) -> None:
    good = TrainState.synthetic(chain_graph)
    bad_params = {
        nid: {p: a.copy() for p, a in bucket.items()}
        for nid, bucket in good.params.items()
    }
    # 篡改 l1.b 的形状（缺一列）。
    bad_params["l1"]["b"] = np.zeros(2)
    with pytest.raises(GraphValidationError) as exc:
        TrainState(
            graph=chain_graph,
            inputs=good.inputs,
            params=bad_params,
            grad_outputs=good.grad_outputs,
        )
    assert exc.value.details["param"] == "b"
