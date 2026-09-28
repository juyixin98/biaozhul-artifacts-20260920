"""Unit tests for training-state lifecycle conflicts."""

from __future__ import annotations

import numpy as np
import pytest

from app.core.errors import InvalidInputError, StateConflictError
from app.core.state import ParameterInit, RunState, RunStatus, TrainingState


def _state() -> TrainingState:
    return TrainingState({"W": ParameterInit(shape=(2, 2), seed=7)})


def _run(state: TrainingState) -> RunState:
    return RunState(
        run_id="r1",
        status=RunStatus.EXECUTED,
        loss=1.0,
        grads={"W": np.ones((2, 2))},
        recomputed_nodes=(),
        forward_flops=10,
        recompute_flops=0,
        backward_flops=10,
        peak_memory=8,
        emitted_side_effects=0,
        rng_replay_ok=True,
    )


@pytest.mark.unit
def test_parameters_initialised_deterministically() -> None:
    a = _state()
    b = _state()
    assert np.array_equal(a.get("W"), b.get("W"))
    assert a.step == 0


@pytest.mark.unit
def test_apply_advances_step_and_updates_without_mutation() -> None:
    state = _state()
    before = state.get("W").copy()
    state.apply_run(_run(state), lr=0.5)
    assert state.step == 1
    # New array, SGD update applied.
    assert np.allclose(state.get("W"), before - 0.5 * np.ones((2, 2)))
    # The captured old array is untouched (immutability).
    assert not np.array_equal(state.get("W"), before)


@pytest.mark.unit
def test_double_apply_is_state_conflict() -> None:
    state = _state()
    run = _run(state)
    state.apply_run(run)
    with pytest.raises(StateConflictError) as exc:
        state.apply_run(run)
    assert exc.value.code == "E_STATE_RUN_DOUBLE_APPLY"
    assert exc.value.category == "state_conflict"


@pytest.mark.unit
def test_apply_rejects_non_executed_run() -> None:
    state = _state()
    bad = RunState(
        run_id="r2", status=RunStatus.FAILED, loss=0.0, grads={},
        recomputed_nodes=(), forward_flops=0, recompute_flops=0,
        backward_flops=0, peak_memory=0, emitted_side_effects=0,
        rng_replay_ok=False,
    )
    with pytest.raises(StateConflictError) as exc:
        state.apply_run(bad)
    assert exc.value.code == "E_STATE_RUN_NOT_EXECUTED"


@pytest.mark.unit
def test_apply_rejects_bad_lr_as_input_error() -> None:
    state = _state()
    with pytest.raises(InvalidInputError):
        state.apply_run(_run(state), lr=0.0)


@pytest.mark.unit
def test_state_requires_at_least_one_parameter() -> None:
    with pytest.raises(InvalidInputError):
        TrainingState({})
