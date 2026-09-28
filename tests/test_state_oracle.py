"""Layer 3b + numerical verification: training state vs independent dense oracle.

Every scenario required for review is here:

* repeated IDs aggregate before the optimiser
* hot/cold rows alternate; cold/untouched rows stay bit-identical
* empty batch changes nothing and does not advance the step
* a very large gradient is handled and cross-checked
* momentum buffer and per-row counters match the independent dense reference

The oracle (``reference_oracle.py``) is a separate plain-Python dense
implementation; only the initial table and hyper-parameters are shared.
"""

from __future__ import annotations

import numpy as np
import pytest

from sparse_embedding.config import ClipMode, OptimizerName
from sparse_embedding.errors import EmptyBatchError, ValidationBatchRejectedError
from sparse_embedding.state import TrainingState
from sparse_embedding.tensors import SparseGradientBatch

from reference_oracle import DenseReferenceOracle

ATOL = 1e-10


def make_state(num_rows=8, dim=3, seed=20260928):
    from sparse_embedding.config import TableSpec

    return TrainingState(TableSpec(num_rows=num_rows, dim=dim), seed=seed)


def send(state, indices, values, *, lr=0.1, momentum=0.9,
         clip_mode=ClipMode.NONE, max_norm=None, wd=0.0,
         name=OptimizerName.SGD_MOMENTUM, run_id="r"):
    from sparse_embedding.config import ClipConfig, OptimizerConfig, TableSpec

    batch = SparseGradientBatch.from_pairs(
        indices, values, TableSpec(state.spec.num_rows, state.spec.dim)
    )
    opt = OptimizerConfig(name=name, lr=lr, momentum=momentum, weight_decay=wd)
    clip = ClipConfig(mode=clip_mode, max_norm=max_norm)
    return state.apply_batch(batch, opt, clip, run_id=run_id)


def paired(num_rows=8, dim=3, **opt_kw):
    """Build a NumPy state and a dense oracle sharing the same start table."""
    state = make_state(num_rows=num_rows, dim=dim)
    oracle = DenseReferenceOracle(
        state.weights.copy(),
        lr=opt_kw.get("lr", 0.1),
        momentum=opt_kw.get("momentum", 0.9),
        weight_decay=opt_kw.get("wd", 0.0),
        clip_mode=opt_kw.get("clip_mode_str", "none"),
        max_norm=opt_kw.get("max_norm"),
    )
    return state, oracle


def assert_matches(state, oracle, *, atol=ATOL):
    np.testing.assert_allclose(state.weights, oracle.weights_matrix(), atol=atol)
    np.testing.assert_allclose(state.momentum, oracle.momentum_matrix(), atol=atol)
    assert state.row_steps.tolist() == oracle.row_steps
    assert state.global_step == oracle.global_step


# ----------------------------------------------------------- required cases


def test_repeated_ids_aggregate_then_match_oracle():
    state, oracle = paired()
    idx = [0, 2, 0, 2, 2]
    val = [[1, 0, 0], [0, 2, 0], [3, 4, 0], [-1, 0, 0], [1, 1, 0]]
    report = send(state, idx, val)
    oracle.run_batch(idx, val)

    # Duplicates collapsed: rows 0 and 2 each stepped exactly once.
    assert report.active_indices.tolist() == [0, 2]
    assert report.raw_row_count == 5
    assert state.row_steps[0] == 1 and state.row_steps[2] == 1
    assert_matches(state, oracle)


def test_hot_cold_rows_alternate_and_cold_untouched_rows_are_frozen():
    state, oracle = paired(num_rows=8, dim=3)
    all_rows = np.arange(8)
    cold_rows = [3, 4, 6, 7]

    before = {r: (state.weights[r].copy(), state.momentum[r].copy()) for r in all_rows}

    # Hot rows 0,1,2,5 alternate across batches; cold rows never appear.
    sequences = [
        ([5, 0], [[1, 1, 1], [2, 0, 1]]),
        ([0, 1, 2], [[1, 0, 0], [0, 1, 0], [0, 0, 1]]),
        ([5, 2], [[2, 2, 2], [1, -1, 0]]),
        ([1, 5, 0], [[3, 3, 3], [-1, 0, 1], [0, 0, 2]]),
    ]
    for i, (idx, val) in enumerate(sequences):
        report = send(state, idx, val, run_id=f"hotcold-{i}")
        oracle.run_batch(idx, val)
        # Cold rows are bit-identical after every batch (no decay, no touch).
        for r in cold_rows:
            np.testing.assert_array_equal(state.weights[r], before[r][0])
            np.testing.assert_array_equal(state.momentum[r], before[r][1])
            assert state.row_steps[r] == 0
        assert set(report.active_indices.tolist()) <= {0, 1, 2, 5}

    assert_matches(state, oracle)
    # Momentum history really accumulated on the hottest row 5.
    assert not np.allclose(state.momentum[5], 0.0)
    assert state.row_steps[5] == 3
    for r in cold_rows:
        assert r not in state.touched_rows


def test_empty_batch_is_noop_and_does_not_advance_step():
    state, oracle = paired()
    w_before = state.weights.copy()
    m_before = state.momentum.copy()
    step_before = state.global_step

    with pytest.raises(EmptyBatchError) as exc:
        send(state, [], [], run_id="empty-1")
    assert exc.value.code == "empty_batch"

    assert state.global_step == step_before
    np.testing.assert_array_equal(state.weights, w_before)
    np.testing.assert_array_equal(state.momentum, m_before)
    assert state.global_step == oracle.global_step == 0


def test_zero_aggregated_gradient_takes_no_step_but_batch_round_advances():
    state, oracle = paired()
    # Row 5 appears twice with cancelling gradients -> aggregated zero.
    idx = [5, 0, 5]
    val = [[2, 2, 2], [1, 1, 1], [-2, -2, -2]]
    report = send(state, idx, val, run_id="zero-row")
    oracle.run_batch(idx, val)

    assert report.active_indices.tolist() == [0]
    assert report.zero_skipped_indices.tolist() == [5]
    # Row 5 took no step: weight/momentum/counter unchanged at init.
    np.testing.assert_array_equal(state.momentum[5], np.zeros(3))
    assert state.row_steps[5] == 0
    # The non-empty batch still counts as one update round.
    assert report.global_step_after == 1
    assert state.global_step == 1
    assert_matches(state, oracle)


def test_large_gradient_matches_oracle_and_stays_finite():
    idx = [1, 1]
    val = [[1e6, -1e6, 5e5], [1e6, 1e6, 5e5]]
    # One consistent pairing at lr=0.01.
    state, oracle = paired(lr=0.01)
    report = send(state, idx, val, lr=0.01, run_id="large")
    oracle.run_batch(idx, val)

    assert np.isfinite(state.weights).all()
    assert np.isfinite(state.momentum).all()
    assert report.active_indices.tolist() == [1]
    # Aggregated [2e6, 0, 1e6]; big but exact in float64.
    np.testing.assert_allclose(report.clipped_gradients[0], [2e6, 0.0, 1e6])
    assert_matches(state, oracle)


def test_momentum_state_persists_across_batches_and_matches_oracle():
    state, oracle = paired(lr=0.1, momentum=0.9)
    idx = [0]
    for step in range(5):
        send(state, idx, [[1.0, 0.0, 0.0]], run_id=f"m-{step}")
        oracle.run_batch(idx, [[1.0, 0.0, 0.0]])
    # v_t = 1 + mu + ... + mu^4 ; row 0 only.
    expected_v0 = sum(0.9 ** k for k in range(5))
    np.testing.assert_allclose(state.momentum[0, 0], expected_v0, atol=1e-10)
    assert state.row_steps[0] == 5
    assert state.row_steps[1] == 0
    assert_matches(state, oracle)


def test_global_clipping_path_matches_oracle():
    state, oracle = paired(clip_mode_str="global", max_norm=2.0)
    idx = [0, 2, 0]
    val = [[5, 0, 0], [0, 4, 0], [5, 0, 0]]
    report = send(state, idx, val, clip_mode=ClipMode.GLOBAL, max_norm=2.0)
    ores = oracle.run_batch(idx, val)
    # global norm of aggregated rows [10,0,0],[0,4,0] = sqrt(116) ~10.77 -> scaled
    assert report.clip_report["applied"] is True
    assert ores.global_norm > 2.0
    assert_matches(state, oracle)


def test_row_clipping_path_matches_oracle():
    state, oracle = paired(clip_mode_str="row", max_norm=2.0)
    idx = [0, 2, 0]
    val = [[5, 0, 0], [0, 4, 0], [5, 0, 0]]
    report = send(state, idx, val, clip_mode=ClipMode.ROW, max_norm=2.0)
    oracle.run_batch(idx, val)
    assert report.clip_report["applied"] is True
    assert_matches(state, oracle)


def test_plain_sgd_path_matches_oracle():
    state, oracle = paired(momentum=0.0)
    idx = [1, 1, 2]
    val = [[1, 1, 1], [1, 1, 1], [2, 0, 0]]
    send(state, idx, val, momentum=0.0, name=OptimizerName.SGD)
    oracle.run_batch(idx, val)
    np.testing.assert_array_equal(state.momentum, np.zeros_like(state.momentum))
    assert_matches(state, oracle)


def test_out_of_range_batch_leaves_state_untouched():
    state, _ = paired()
    w = state.weights.copy()
    with pytest.raises(ValidationBatchRejectedError):
        send(state, [0, 999], [[1, 1, 1], [1, 1, 1]])
    np.testing.assert_array_equal(state.weights, w)
    assert state.global_step == 0
