"""Unit tests for the data cursor, including epoch-boundary behavior."""

import numpy as np
import pytest

from checkpoint_service.cursor import DataCursor


@pytest.mark.unit
def test_cursor_visits_each_index_once_per_epoch() -> None:
    rng = np.random.default_rng(0)
    cur = DataCursor.fresh(10, rng)
    seen: list[int] = []
    for _ in range(10 // 2):
        idx = cur.next_indices(2, rng)
        seen.extend(int(i) for i in idx)
    assert sorted(seen) == list(range(10))
    assert cur.epoch == 0
    assert cur.pos == 10


@pytest.mark.unit
def test_cursor_rolls_over_epoch_and_stays_at_batch_end_boundary() -> None:
    rng = np.random.default_rng(1)
    n, bs = 10, 4  # 2 full batches, then 2 leftover samples
    cur = DataCursor.fresh(n, rng)
    cur.next_indices(bs, rng)  # pos 4
    cur.next_indices(bs, rng)  # pos 8
    assert cur.epoch == 0
    # Next batch (4) does not fit: a new epoch permutation is drawn and the
    # cursor restarts at pos 0 BEFORE taking the batch.
    idx = cur.next_indices(bs, rng)
    assert cur.epoch == 1
    assert cur.pos == bs
    assert len(idx) == bs
    assert set(int(i) for i in idx).issubset(set(range(n)))


@pytest.mark.unit
def test_cursor_roundtrip_state_preserves_permutation() -> None:
    rng = np.random.default_rng(4)
    cur = DataCursor.fresh(12, rng)
    cur.next_indices(3, rng)
    cur.next_indices(3, rng)
    restored = DataCursor.from_state(cur.to_state())
    np.testing.assert_array_equal(restored.permutation, cur.permutation)
    assert restored.pos == cur.pos
    assert restored.epoch == cur.epoch
    assert restored.global_step == cur.global_step


@pytest.mark.unit
def test_cursor_rejects_invalid_permutation() -> None:
    good_rng = np.random.default_rng(0)
    state = DataCursor.fresh(4, good_rng).to_state()
    state["permutation"] = np.array([0, 1, 2, 2], dtype=np.int64)  # duplicate
    with pytest.raises(ValueError):
        DataCursor.from_state(state)


@pytest.mark.unit
def test_cursor_rejects_missing_field_and_bad_pos() -> None:
    rng = np.random.default_rng(0)
    state = DataCursor.fresh(4, rng).to_state()
    del state["pos"]
    with pytest.raises(ValueError):
        DataCursor.from_state(state)

    state = DataCursor.fresh(4, rng).to_state()
    state["pos"] = 99
    with pytest.raises(ValueError):
        DataCursor.from_state(state)
