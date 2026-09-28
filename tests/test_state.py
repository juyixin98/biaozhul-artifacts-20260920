"""Tests for training state: parameter ownership, optimizer, phase conflicts.

Optimizer expected values are computed independently here from snapshots;
TrainingState itself is the code under test and is not used to generate its
own expected answers.
"""

from __future__ import annotations

import numpy as np
import pytest

from tensor_mem import fixtures as fx
from tensor_mem.errors import (
    ComputationError,
    InputValidationError,
    StateConflictError,
)
from tensor_mem.executor import Executor
from tensor_mem.state import Phase, TrainingState


@pytest.mark.unit
def test_unknown_optimizer_is_input_error():
    with pytest.raises(InputValidationError) as exc:
        TrainingState("lamb")
    assert exc.value.details["supported"] == ["sgd", "adam"]


@pytest.mark.unit
def test_duplicate_parameter_is_state_conflict():
    s = TrainingState()
    s.declare_parameter("W", "float32", (2, 2))
    with pytest.raises(StateConflictError) as exc:
        s.declare_parameter("W", "float32", (2, 2))
    assert exc.value.details["parameter"] == "W"


@pytest.mark.unit
def test_phase_transitions_are_enforced():
    s = TrainingState()
    s.declare_parameter("W", "float32", (2, 2))
    with pytest.raises(StateConflictError):
        s.parameter_array("W")          # not prepared
    s.prepare(seed=1)
    assert s.phase is Phase.PREPARED
    with pytest.raises(StateConflictError):
        s.declare_parameter("b", "float32", (2,))  # locked after prepare
    s.close()
    assert s.phase is Phase.CLOSED
    with pytest.raises(StateConflictError):
        s.begin_step()                  # closed


@pytest.mark.unit
def test_prepare_without_parameters_is_conflict():
    s = TrainingState()
    with pytest.raises(StateConflictError):
        s.prepare()


@pytest.mark.unit
def test_double_begin_step_is_conflict_and_close_guard():
    s = TrainingState()
    s.declare_parameter("W", "float32", (2, 2))
    s.prepare()
    s.begin_step()
    with pytest.raises(StateConflictError):
        s.begin_step()
    with pytest.raises(StateConflictError):
        s.close()                        # step still open
    s.apply_gradients({"W": np.zeros((2, 2), np.float32)}, lr=0.1)
    s.close()


@pytest.mark.unit
def test_apply_without_begin_is_conflict_and_gradient_set_mismatch():
    s = TrainingState()
    s.declare_parameter("W", "float32", (2, 2))
    s.declare_parameter("b", "float32", (2,))
    s.prepare()
    with pytest.raises(StateConflictError):
        s.apply_gradients({"W": np.zeros((2, 2), np.float32)}, lr=0.1)
    s.begin_step()
    with pytest.raises(StateConflictError) as exc:
        s.apply_gradients({"W": np.zeros((2, 2), np.float32)}, lr=0.1)  # b missing
    assert exc.value.details["missing"] == ["b"]


@pytest.mark.unit
def test_gradient_shape_divergence_is_state_conflict():
    s = TrainingState()
    s.declare_parameter("W", "float32", (2, 2))
    s.prepare()
    s.begin_step()
    with pytest.raises(StateConflictError) as exc:
        s.apply_gradients({"W": np.zeros((3, 2), np.float32)}, lr=0.1)
    assert exc.value.details["expected"] == [2, 2]
    assert exc.value.details["got"] == [3, 2]


@pytest.mark.unit
def test_non_positive_lr_input_error_and_non_finite_gradient_computation_error():
    s = TrainingState()
    s.declare_parameter("W", "float32", (2, 2))
    s.prepare()
    s.begin_step()
    with pytest.raises(InputValidationError):
        s.apply_gradients({"W": np.zeros((2, 2), np.float32)}, lr=0.0)
    with pytest.raises(ComputationError) as exc:
        s.apply_gradients(
            {"W": np.full((2, 2), np.nan, np.float32)}, lr=0.1
        )
    assert exc.value.details["parameter"] == "W"
    assert exc.value.details["non_finite"] == 4


@pytest.mark.unit
def test_sgd_update_matches_independent_formula():
    s = TrainingState("sgd")
    s.declare_parameter("W", "float32", (3, 2))
    s.prepare(seed=42)
    w0 = s.snapshot()["W"].copy()
    grad = np.arange(6, dtype=np.float32).reshape(3, 2)
    lr = 0.25
    s.begin_step()
    s.apply_gradients({"W": grad}, lr=lr)
    expected = (w0 - lr * grad).astype(np.float32)
    np.testing.assert_allclose(s.parameter_array("W"), expected, rtol=1e-6)
    assert s.step_count == 1


@pytest.mark.unit
def test_adam_update_matches_independent_formula():
    s = TrainingState("adam")
    s.declare_parameter("W", "float32", (2, 2))
    s.prepare(seed=7)
    w0 = s.snapshot()["W"].copy()
    grad = (np.arange(4, dtype=np.float32).reshape(2, 2) - 1.0)
    lr, b1, b2, eps = 0.01, 0.9, 0.999, 1e-8

    # independent expectation for one Adam step
    m = (1 - b1) * grad
    v = (1 - b2) * grad * grad
    mhat = m / (1 - b1)
    vhat = v / (1 - b2)
    expected = w0 - lr * mhat / (np.sqrt(vhat) + eps)

    s.begin_step()
    s.apply_gradients({"W": grad}, lr=lr, beta1=b1, beta2=b2, eps=eps)
    np.testing.assert_allclose(s.parameter_array("W"), expected, rtol=1e-6)


@pytest.mark.unit
def test_state_bytes_accounts_for_parameters_gradients_and_slots():
    s = TrainingState("adam")
    s.declare_parameter("W", "float32", (4, 4))   # 64 bytes
    s.prepare()
    bytes_ = s.state_bytes()
    assert bytes_["parameters"] == 64
    assert bytes_["gradients"] == 64
    assert bytes_["optimizer_slots"] == 128       # m and v
    assert bytes_["total"] == 256


@pytest.mark.integration
def test_graph_consumes_state_parameter_as_external_feed_and_applies_grad():
    graph = fx.build_linear_grad_graph()
    state = TrainingState("sgd")
    state.declare_parameter("W", "float32", (3, 4))
    state.prepare(seed=123)
    w0 = state.snapshot()["W"].copy()

    ex = Executor(graph, external_names=frozenset({"W"}))
    case = fx.linear_grad_case(seed=9)
    feeds = dict(case.feeds)
    feeds["W"] = state.parameter_array("W")

    state.begin_step()
    handles, report = ex.execute(feeds, run_id="train-external")
    # external parameter never enters the reusable pool (48B aligned to 64)
    assert report.wave_resident[0]["external_bytes"] == 64
    grad = handles["grad_W"].array
    y = handles["y"].array
    np.testing.assert_allclose(y, w0 @ feeds["x"], rtol=1e-6)
    state.apply_gradients({"W": grad}, lr=0.05)
    np.testing.assert_allclose(
        state.parameter_array("W"), w0 - 0.05 * grad, rtol=1e-6
    )
    for h in handles.values():
        h.release()
