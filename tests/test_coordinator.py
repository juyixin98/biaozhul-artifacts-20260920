"""Coordinator state-machine tests against the independent reference oracle.

These tests assert *concrete results* (exact reduced values, generation
numbers, machine-readable failure categories), not merely that an API is
callable.  Expected answers always come from
:mod:`bucket_sync.reference`, which re-derives the math without importing
the reducer under test.
"""

from __future__ import annotations

import numpy as np
import pytest

from bucket_sync.bucketing import BucketLayout
from bucket_sync.config import make_graph_with_frozen_bias
from bucket_sync.coordinator import (
    Coordinator,
    RejectReason,
    RoundStatus,
)
from bucket_sync.diagnostics import Diagnostics
from bucket_sync.reference import (
    joint_linear_mse,
    reference_sgd_step,
    reference_weighted_mean,
)
from bucket_sync.training import BIAS_PARAM, WEIGHT_PARAM, ModelState

from tests.drivers import (
    bucket_segments,
    local_gradient_sums,
    register_all,
    submit_round,
)

pytestmark = pytest.mark.unit

WORKERS = ["w0", "w1", "w2"]
SIZES = (5, 3, 2)
TOL = 1e-10


def _joint_x_y(shards):
    return np.vstack([s[0] for s in shards]), np.vstack([s[1] for s in shards])


def _reference_committed_params(coordinator, desc, locals_by_worker, layout, lr):
    """Independent expectation for one committed round."""
    contribs = [(grads, n) for grads, n in locals_by_worker.values()]
    mean_grads = reference_weighted_mean(contribs, [WEIGHT_PARAM, BIAS_PARAM])
    return reference_sgd_step(desc["base_params"], mean_grads, lr)


def _naive_equal_weight_mean(locals_by_worker):
    # The deliberately-wrong baseline: average of per-worker means.
    means = {}
    for grads, n in locals_by_worker.values():
        for name, g in grads.items():
            means.setdefault(name, []).append(g / n)
    return {name: np.mean(gs, axis=0) for name, gs in means.items()}


# ---------------------------------------------------------------------------
# 1. fixed layout / ordering independence
# ---------------------------------------------------------------------------


def test_different_completion_orders_produce_identical_results(
    coordinator: Coordinator, layout, unequal_shards, cfg
):
    x_joint, y_joint = _joint_x_y(unequal_shards)
    final_per_order = []
    evidence_hashes = []
    orders = {
        "forward": {wid: [0, 1] for wid in WORKERS},
        "reverse": {wid: [1, 0] for wid in WORKERS},
        "mixed": {"w0": [1, 0], "w1": [0, 1], "w2": [1, 0]},
    }
    for tag, order in orders.items():
        diag = Diagnostics()
        c = Coordinator(
            coordinator._graph,
            layout,
            ModelState(coordinator._graph, coordinator.snapshot_params()),
            lr=cfg.lr,
            diagnostics=diag,
            heartbeat_timeout=cfg.heartbeat_timeout_s,
        )
        register_all(c, WORKERS)
        desc = c.begin_round(WORKERS)
        token = desc["round_id"], desc["base_token"]
        locals_ = {}
        for wid, shard in zip(WORKERS, unequal_shards):
            grads, n = local_gradient_sums(desc["base_params"], shard)
            locals_[wid] = (grads, n)
            segs = bucket_segments(layout, grads)
            for bi in order[wid]:
                seg, mask = segs[bi]
                res = c.submit_bucket(
                    token[0], wid, bi, seg, n, token[1], present_mask=mask
                )
                assert res.accepted, (tag, wid, bi)
                # weights never move before commit
                assert c.current_generation() == 0
        report = c.commit_round()
        assert report.outcome == "committed"
        final_per_order.append(c.snapshot_params())
        evidence_hashes.append(tuple(e.reduced_hash12 for e in report.bucket_evidence))

    for params in final_per_order[1:]:
        np.testing.assert_allclose(params[WEIGHT_PARAM], final_per_order[0][WEIGHT_PARAM], atol=TOL)
        np.testing.assert_allclose(params[BIAS_PARAM], final_per_order[0][BIAS_PARAM], atol=TOL)
    assert evidence_hashes.count(evidence_hashes[0]) == len(evidence_hashes)


def test_result_equals_single_process_joint_batch(
    coordinator: Coordinator, layout, unequal_shards, cfg
):
    desc, locals_ = submit_round(coordinator, WORKERS, unequal_shards, layout)
    report = coordinator.commit_round()
    assert report.outcome == "committed"

    x_joint, y_joint = _joint_x_y(unequal_shards)
    joint_mean = joint_linear_mse(desc["base_params"], x_joint, y_joint)
    expected = reference_sgd_step(desc["base_params"], joint_mean, cfg.lr)
    actual = coordinator.snapshot_params()
    np.testing.assert_allclose(actual[WEIGHT_PARAM], expected[WEIGHT_PARAM], atol=TOL)
    np.testing.assert_allclose(actual[BIAS_PARAM], expected[BIAS_PARAM], atol=TOL)


def test_weighted_result_differs_from_naive_average_of_averages(
    coordinator: Coordinator, layout, unequal_shards
):
    desc, locals_ = submit_round(coordinator, WORKERS, unequal_shards, layout)
    coordinator.commit_round()
    actual = coordinator.snapshot_params()

    naive = reference_sgd_step(
        desc["base_params"], _naive_equal_weight_mean(locals_), lr=0.05
    )
    # Unequal batch sizes (5,3,2) make the naive answer provably wrong.
    assert not np.allclose(actual[WEIGHT_PARAM], naive[WEIGHT_PARAM], atol=1e-8)
    assert not np.allclose(actual[BIAS_PARAM], naive[BIAS_PARAM], atol=1e-8)


# ---------------------------------------------------------------------------
# 2. bucket generations and reduction evidence
# ---------------------------------------------------------------------------


def test_bucket_evidence_records_generation_and_sample_basis(
    coordinator: Coordinator, layout, unequal_shards
):
    desc, locals_ = submit_round(coordinator, WORKERS, unequal_shards, layout)
    report = coordinator.commit_round()
    assert report.generation_before == 0
    assert report.generation_after == 1
    assert len(report.bucket_evidence) == layout.bucket_count()
    for e in report.bucket_evidence:
        assert e.generation == 0  # all buckets reduced from the same base gen
        assert tuple(e.contributors) == tuple(WORKERS)
        assert tuple(e.samples_per_worker) == SIZES
        assert e.total_samples == sum(SIZES)
        assert e.cover_min == e.cover_max == len(WORKERS)
        assert "sum_w n_w" in e.weight_basis


def test_reduced_hashes_match_independent_per_bucket_recomputation(
    coordinator: Coordinator, layout, unequal_shards
):
    desc, locals_ = submit_round(coordinator, WORKERS, unequal_shards, layout)
    report = coordinator.commit_round()
    for e in report.bucket_evidence:
        bucket = layout.buckets[e.bucket_index]
        # Independent recomputation: workers report gradient SUMS, so the
        # reducer divides their sum by the true joint sample count.
        acc = np.zeros(bucket.size)
        for (grads, n) in locals_.values():
            segs = bucket_segments(layout, grads)
            acc += segs[e.bucket_index][0]
        acc /= float(sum(SIZES))
        assert e.reduced_norm == pytest.approx(float(np.linalg.norm(acc)), abs=TOL)


# ---------------------------------------------------------------------------
# 3. no early updates / base generation barrier
# ---------------------------------------------------------------------------


def test_completed_buckets_do_not_move_weights_before_commit(
    coordinator: Coordinator, layout, unequal_shards
):
    register_all(coordinator, WORKERS)
    desc = coordinator.begin_round(WORKERS)
    base_before = coordinator.snapshot_params()
    for wid, shard in zip(WORKERS, unequal_shards):
        grads, n = local_gradient_sums(desc["base_params"], shard)
        segs = bucket_segments(layout, grads)
        for bi, (seg, mask) in segs.items():
            res = coordinator.submit_bucket(
                desc["round_id"], wid, bi, seg, n, desc["base_token"], mask
            )
        # Even after this worker completed all its buckets, generation is 0.
        assert coordinator.current_generation() == 0
        np.testing.assert_allclose(
            coordinator.snapshot_params()[WEIGHT_PARAM], base_before[WEIGHT_PARAM]
        )
    report = coordinator.commit_round()
    assert report.outcome == "committed"
    assert coordinator.current_generation() == 1


def test_stale_base_token_is_rejected(coordinator: Coordinator, layout, unequal_shards):
    # Round 1 via the standard driver (it begins, submits, but does not commit).
    first, _ = submit_round(coordinator, WORKERS, unequal_shards, layout)
    coordinator.commit_round()
    second = coordinator.begin_round(WORKERS)
    grads, n = local_gradient_sums(second["base_params"], unequal_shards[0])
    seg, mask = bucket_segments(layout, grads)[0]
    result = coordinator.submit_bucket(
        second["round_id"], "w0", 0, seg, n,
        first["base_token"], present_mask=mask,  # token from generation 0
    )
    assert not result.accepted
    assert result.reason == RejectReason.WRONG_BASE_GENERATION.value
    # the diagnostic record explains why and carries both tokens
    events = [e for e in coordinator.diagnostics.events() if e.record_id == result.record_id]
    assert len(events) == 1
    assert events[0].reason == RejectReason.WRONG_BASE_GENERATION.value
    assert events[0].detail["got_token"] == first["base_token"]


# ---------------------------------------------------------------------------
# 4. missing gradients: explicit, refused in strict mode, handled in partial
# ---------------------------------------------------------------------------


def _submit_with_missing_bias(c, layout, shards, drop_wid):
    register_all(c, WORKERS)
    desc = c.begin_round(WORKERS)
    locals_ = {}
    for wid, shard in zip(WORKERS, shards):
        grads, n = local_gradient_sums(
            desc["base_params"], shard, drop_params=(BIAS_PARAM,) if wid == drop_wid else ()
        )
        locals_[wid] = (grads, n)
        segs = bucket_segments(layout, grads)
        for bi, (seg, mask) in segs.items():
            c.submit_bucket(desc["round_id"], wid, bi, seg, n, desc["base_token"], mask)
    return desc, locals_


def test_missing_gradient_is_undecided_not_silently_zero(
    coordinator: Coordinator, layout, unequal_shards
):
    _submit_with_missing_bias(coordinator, layout, unequal_shards, "w1")
    report = coordinator.commit_round()
    assert report.outcome == "undecided"
    assert report.reason == RejectReason.MISSING_GRADIENT.value
    assert "bucket_1" in report.detail["slots_without_full_coverage"]
    # weights untouched while undecided
    assert coordinator.current_generation() == 0


def test_missing_gradient_declared_nonzero_is_rejected_bad_mask(
    coordinator: Coordinator, layout, unequal_shards
):
    register_all(coordinator, WORKERS)
    desc = coordinator.begin_round(WORKERS)
    grads, n = local_gradient_sums(desc["base_params"], unequal_shards[1])
    segs = bucket_segments(layout, grads)
    seg, mask = segs[1]  # bucket 1 = [w2, b]
    mask = mask.copy()
    mask[-1] = False  # claim bias absent...
    # ...but segment still carries a nonzero bias value
    result = coordinator.submit_bucket(
        desc["round_id"], "w1", 1, seg, n, desc["base_token"], mask
    )
    assert not result.accepted
    assert result.reason == RejectReason.BAD_MASK.value


def test_partial_coverage_reduces_per_slot_over_covering_workers(
    graph, layout, unequal_shards, cfg
):
    diag = Diagnostics()
    model = ModelState(graph, __import__("bucket_sync.config", fromlist=["initial_params"]).initial_params(cfg.in_features))
    c = Coordinator(
        graph, layout, model, lr=cfg.lr, diagnostics=diag,
        heartbeat_timeout=cfg.heartbeat_timeout_s, allow_partial_coverage=True,
    )
    desc, locals_ = _submit_with_missing_bias(c, layout, unequal_shards, "w1")
    report = c.commit_round()
    assert report.outcome == "committed"
    # w: all three workers (5+3+2); b: only w0,w2 (5+2)
    w_mean = reference_weighted_mean(
        [locals_[w] for w in WORKERS], [WEIGHT_PARAM]
    )
    b_mean = reference_weighted_mean(
        [locals_[w] for w in ("w0", "w2")], [BIAS_PARAM]
    )
    expected = reference_sgd_step(
        desc["base_params"],
        {WEIGHT_PARAM: w_mean[WEIGHT_PARAM], BIAS_PARAM: b_mean[BIAS_PARAM]},
        cfg.lr,
    )
    actual = c.snapshot_params()
    np.testing.assert_allclose(actual[WEIGHT_PARAM], expected[WEIGHT_PARAM], atol=TOL)
    np.testing.assert_allclose(actual[BIAS_PARAM], expected[BIAS_PARAM], atol=TOL)
    b_evidence = [e for e in report.bucket_evidence if e.bucket_index == 1][0]
    assert b_evidence.cover_min == 2
    assert b_evidence.cover_max == 3
    assert b_evidence.sample_weight_min == 7.0  # bias covers 5+2 samples


# ---------------------------------------------------------------------------
# 5. lost worker aborts the round
# ---------------------------------------------------------------------------


def test_worker_loss_aborts_round_and_rejects_late_submissions(
    coordinator: Coordinator, layout, unequal_shards
):
    register_all(coordinator, WORKERS)
    desc = coordinator.begin_round(WORKERS)
    # w0 and w1 finish; w2 is still in flight when w1 dies.
    for wid, shard in zip(("w0", "w1"), unequal_shards[:2]):
        grads, n = local_gradient_sums(desc["base_params"], shard)
        for bi, (seg, mask) in bucket_segments(layout, grads).items():
            assert coordinator.submit_bucket(
                desc["round_id"], wid, bi, seg, n, desc["base_token"], mask
            ).accepted
    coordinator.mark_worker_lost("w1", reason="simulated_exit")
    assert coordinator.round_descriptor()["status"] == RoundStatus.ABORTED.value

    # w2's late work is refused for the specific reason.
    grads2, n2 = local_gradient_sums(desc["base_params"], unequal_shards[2])
    seg, mask = bucket_segments(layout, grads2)[0]
    late = coordinator.submit_bucket(
        desc["round_id"], "w2", 0, seg, n2, desc["base_token"], mask
    )
    assert not late.accepted
    assert late.reason == RejectReason.ROUND_ABORTED.value

    report = coordinator.commit_round()
    assert report.outcome == "rejected"
    assert report.reason == RejectReason.ROUND_ABORTED.value
    assert coordinator.current_generation() == 0  # nothing applied


def test_heartbeat_timeout_is_detected_and_aborts(graph, layout, model, cfg):
    clock = {"t": 100.0}
    c = Coordinator(
        graph, layout, model, lr=cfg.lr,
        time_func=lambda: clock["t"], heartbeat_timeout=cfg.heartbeat_timeout_s,
    )
    register_all(c, WORKERS)
    c.begin_round(WORKERS)
    assert c.check_liveness() == set()
    clock["t"] += cfg.heartbeat_timeout_s + 0.1
    lost = c.check_liveness()
    assert lost == set(WORKERS)
    assert c.round_descriptor()["status"] == RoundStatus.ABORTED.value


def test_dead_worker_cannot_join_a_round(coordinator: Coordinator):
    register_all(coordinator, WORKERS)
    coordinator.mark_worker_lost("w0")
    with pytest.raises(ValueError, match="dead workers"):
        coordinator.begin_round(WORKERS)


# ---------------------------------------------------------------------------
# 6. failure categories
# ---------------------------------------------------------------------------


def _open(coordinator: Coordinator):
    register_all(coordinator, WORKERS)
    return coordinator.begin_round(WORKERS)


def test_reject_unknown_worker(coordinator: Coordinator, layout):
    register_all(coordinator, WORKERS)
    desc = coordinator.begin_round(WORKERS)
    res = coordinator.submit_bucket(
        desc["round_id"], "stranger", 0, np.zeros(2), 1,
        desc["base_token"], np.ones(2, dtype=bool),
    )
    assert res.reason == RejectReason.UNKNOWN_WORKER.value


def test_reject_non_participant(coordinator: Coordinator, layout):
    register_all(coordinator, ["w0", "w1", "w2", "bench"])
    desc = coordinator.begin_round(WORKERS)
    res = coordinator.submit_bucket(
        desc["round_id"], "bench", 0, np.zeros(2), 1,
        desc["base_token"], np.ones(2, dtype=bool),
    )
    assert res.reason == RejectReason.NOT_PARTICIPANT.value


def test_reject_wrong_round(coordinator: Coordinator, layout):
    desc = _open(coordinator)
    res = coordinator.submit_bucket(
        desc["round_id"] + 99, "w0", 0, np.zeros(2), 1,
        desc["base_token"], np.ones(2, dtype=bool),
    )
    assert res.reason == RejectReason.WRONG_ROUND.value


def test_reject_submission_without_open_round(coordinator: Coordinator, layout):
    register_all(coordinator, WORKERS)
    res = coordinator.submit_bucket(1, "w0", 0, np.zeros(2), 1, "x", np.ones(2, dtype=bool))
    assert res.reason == RejectReason.ROUND_NOT_OPEN.value


def test_reject_unknown_bucket_and_bad_shape(coordinator: Coordinator, layout):
    desc = _open(coordinator)
    res_bad_index = coordinator.submit_bucket(
        desc["round_id"], "w0", 99, np.zeros(2), 1,
        desc["base_token"], np.ones(2, dtype=bool),
    )
    assert res_bad_index.reason == RejectReason.UNKNOWN_BUCKET.value
    res_bad_shape = coordinator.submit_bucket(
        desc["round_id"], "w0", 0, np.zeros(3), 1,
        desc["base_token"], np.ones(3, dtype=bool),
    )
    assert res_bad_shape.reason == RejectReason.BAD_SHAPE.value


def test_reject_non_finite_and_bad_sample_count(coordinator: Coordinator, layout):
    desc = _open(coordinator)
    res_nan = coordinator.submit_bucket(
        desc["round_id"], "w0", 0, np.array([np.nan, 0.0]), 1,
        desc["base_token"], np.ones(2, dtype=bool),
    )
    assert res_nan.reason == RejectReason.NON_FINITE.value
    res_n = coordinator.submit_bucket(
        desc["round_id"], "w0", 0, np.zeros(2), 0,
        desc["base_token"], np.ones(2, dtype=bool),
    )
    assert res_n.reason == RejectReason.INVALID_SAMPLE_COUNT.value


def test_reject_duplicate_bucket_and_sample_count_mismatch(
    coordinator: Coordinator, layout, unequal_shards
):
    desc = _open(coordinator)
    grads, n = local_gradient_sums(desc["base_params"], unequal_shards[0])
    seg, mask = bucket_segments(layout, grads)[0]
    first = coordinator.submit_bucket(
        desc["round_id"], "w0", 0, seg, n, desc["base_token"], mask
    )
    assert first.accepted
    dup = coordinator.submit_bucket(
        desc["round_id"], "w0", 0, seg, n, desc["base_token"], mask
    )
    assert dup.reason == RejectReason.DUPLICATE_BUCKET.value
    seg1, mask1 = bucket_segments(layout, grads)[1]
    mismatch = coordinator.submit_bucket(
        desc["round_id"], "w0", 1, seg1, n + 1, desc["base_token"], mask1
    )
    assert mismatch.reason == RejectReason.SAMPLE_COUNT_MISMATCH.value


def test_reject_mask_claiming_placeholder_slot(cfg):
    graph = make_graph_with_frozen_bias(cfg.in_features)
    layout = BucketLayout(graph, cfg.bucket_size)
    from bucket_sync.config import initial_params
    from bucket_sync.training import ModelState
    model = ModelState(graph, initial_params(cfg.in_features))
    c = Coordinator(graph, layout, model, lr=cfg.lr)
    register_all(c, WORKERS)
    desc = c.begin_round(WORKERS)
    # bucket 1 = [w2, b(placeholder)]; claim coverage on b
    res = c.submit_bucket(
        desc["round_id"], "w0", 1, np.zeros(2), 3, desc["base_token"],
        np.array([True, True]),
    )
    assert res.reason == RejectReason.BAD_MASK.value


def test_reject_nonzero_value_on_placeholder(cfg):
    graph = make_graph_with_frozen_bias(cfg.in_features)
    layout = BucketLayout(graph, cfg.bucket_size)
    from bucket_sync.config import initial_params
    from bucket_sync.training import ModelState
    model = ModelState(graph, initial_params(cfg.in_features))
    c = Coordinator(graph, layout, model, lr=cfg.lr)
    register_all(c, WORKERS)
    desc = c.begin_round(WORKERS)
    res = c.submit_bucket(
        desc["round_id"], "w0", 1, np.array([0.0, 1.25]), 3, desc["base_token"],
        np.array([True, False]),
    )
    assert res.reason == RejectReason.PLACEHOLDER_NONZERO.value


def test_skipped_worker_contributes_no_weight_and_others_match_reference(
    coordinator: Coordinator, layout, unequal_shards, cfg
):
    register_all(coordinator, WORKERS)
    desc = coordinator.begin_round(WORKERS)
    coordinator.skip_round("w2")  # idle this round
    for wid, shard in zip(("w0", "w1"), unequal_shards[:2]):
        grads, n = local_gradient_sums(desc["base_params"], shard)
        for bi, (seg, mask) in bucket_segments(layout, grads).items():
            assert coordinator.submit_bucket(
                desc["round_id"], wid, bi, seg, n, desc["base_token"], mask
            ).accepted
    idle_res = coordinator.submit_bucket(
        desc["round_id"], "w2", 0, np.zeros(2), 1, desc["base_token"],
        np.ones(2, dtype=bool),
    )
    assert idle_res.reason == RejectReason.WORKER_SKIPPED_ROUND.value
    report = coordinator.commit_round()
    assert report.outcome == "committed"
    x_joint = np.vstack([s[0] for s in unequal_shards[:2]])
    y_joint = np.vstack([s[1] for s in unequal_shards[:2]])
    expected = reference_sgd_step(
        desc["base_params"], joint_linear_mse(desc["base_params"], x_joint, y_joint), cfg.lr
    )
    actual = coordinator.snapshot_params()
    np.testing.assert_allclose(actual[WEIGHT_PARAM], expected[WEIGHT_PARAM], atol=TOL)
    np.testing.assert_allclose(actual[BIAS_PARAM], expected[BIAS_PARAM], atol=TOL)
    assert report.bucket_evidence[0].total_samples == 8


def test_frozen_parameter_is_not_updated(cfg):
    graph = make_graph_with_frozen_bias(cfg.in_features)
    layout = BucketLayout(graph, cfg.bucket_size)
    from bucket_sync.config import initial_params
    from bucket_sync.training import ModelState
    model = ModelState(graph, initial_params(cfg.in_features))
    c = Coordinator(graph, layout, model, lr=cfg.lr)
    shards = __import__("bucket_sync.config", fromlist=["make_shards"]).make_shards(
        cfg.in_features, SIZES
    )
    register_all(c, WORKERS)
    desc = c.begin_round(WORKERS)
    for wid, shard in zip(WORKERS, shards):
        grads, n = local_gradient_sums(desc["base_params"], shard)
        for bi, (seg, mask) in bucket_segments(layout, grads).items():
            assert c.submit_bucket(
                desc["round_id"], wid, bi, seg, n, desc["base_token"], mask
            ).accepted
    report = c.commit_round()
    assert report.outcome == "committed"
    actual = c.snapshot_params()
    np.testing.assert_allclose(actual[BIAS_PARAM], desc["base_params"][BIAS_PARAM])


# ---------------------------------------------------------------------------
# 7. multi-round equivalence with the independent multi-round oracle
# ---------------------------------------------------------------------------


def test_three_rounds_match_reference_run(coordinator: Coordinator, layout, unequal_shards, cfg):
    from bucket_sync.reference import reference_run

    init = coordinator.snapshot_params()
    for _ in range(3):
        desc, locals_ = submit_round(coordinator, WORKERS, unequal_shards, layout)
        report = coordinator.commit_round()
        assert report.outcome == "committed"

    expected = reference_run(init, unequal_shards, lr=cfg.lr, rounds=3)
    actual = coordinator.snapshot_params()
    np.testing.assert_allclose(actual[WEIGHT_PARAM], expected[WEIGHT_PARAM], atol=1e-9)
    np.testing.assert_allclose(actual[BIAS_PARAM], expected[BIAS_PARAM], atol=1e-9)
    assert coordinator.current_generation() == 3
