"""Training-state lifecycle tests."""
from __future__ import annotations

import numpy as np
import pytest


from tenmem.errors import StateConflictError
from tenmem.state import TrainingState

pytestmark = pytest.mark.unit


def _w(seed: int) -> dict[str, np.ndarray]:
    rng = np.random.default_rng(seed)
    return {"w": rng.normal(size=(4, 4)).astype(np.float32)}


def test_save_and_restore_returns_independent_copy() -> None:
    state = TrainingState()
    w = _w(1)
    ckpt = state.save(w, step=0)
    restored = state.restore()
    np.testing.assert_array_equal(restored["w"], w["w"])
    # Mutating the restored copy must not corrupt checkpoint history.
    restored["w"][:] = 999.0
    again = state.restore()
    assert not np.allclose(again["w"], restored["w"])
    np.testing.assert_array_equal(again["w"], w["w"])


def test_restore_unknown_checkpoint_is_state_conflict() -> None:
    state = TrainingState()
    with pytest.raises(StateConflictError, match="no checkpoint"):
        state.restore()
    state.save(_w(1), step=0)
    with pytest.raises(StateConflictError, match="unknown checkpoint"):
        state.restore("ckpt-missing")


def test_refusing_to_go_backwards_in_step() -> None:
    state = TrainingState()
    state.save(_w(1), step=10)
    with pytest.raises(StateConflictError, match="older than latest"):
        state.save(_w(2), step=9)


def test_empty_checkpoint_rejected() -> None:
    state = TrainingState()
    with pytest.raises(StateConflictError, match="empty checkpoint"):
        state.save({}, step=0)


def test_rollback_removes_newer_snapshots() -> None:
    state = TrainingState()
    c0 = state.save(_w(0), step=0)
    state.save(_w(1), step=1)
    state.save(_w(2), step=2)
    kept = state.rollback_to(c0.checkpoint_id)
    assert kept.step == 0
    assert state.latest().step == 0
    with pytest.raises(StateConflictError):
        state.rollback_to("does-not-exist")
