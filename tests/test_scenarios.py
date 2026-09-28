"""场景级测试：通过 runtime.Scenario 穷尽四类边界。

与 test_training 的区别：这里断言*提交全过程*观察到的世代/权重序列、
最终判定类别，以及所有代表完成顺序下的逐位等价性。
"""

from __future__ import annotations

import numpy as np
import pytest

from gradbucket.diagnostics import Verdict
from gradbucket.graph import LinearModel, ModelConfig
from gradbucket.reference import union_batch_gradient
from gradbucket.runtime import (
    Scenario,
    representative_orders,
    run_scenario,
)


def covered_reference(res):
    """逐参数"覆盖者联合批"参照（独立于 reducer）。"""
    refs = {}
    for p in res.commit.gradients:
        workers = [w for w in sorted(res.shards)
                   if p not in res.omit_params.get(w, set())]
        g, n = union_batch_gradient(
            LinearModel(ModelConfig()),
            [(res.shards[w][0], res.shards[w][1]) for w in workers],
        )
        refs[p] = (g[p], n, workers)
    return refs


def assert_generation_frozen_during_submission(res):
    # 任何桶（含收齐的桶）都不得提前推进世代。
    assert res.generations_seen_during_submission, "场景无提交记录"
    assert set(res.generations_seen_during_submission) == {0}
    # 权重快照在提交阶段全部保持轮初零值。
    for snap in res.weights_seen_during_submission:
        for v in snap.values():
            np.testing.assert_array_equal(v, np.zeros_like(v))


@pytest.mark.parametrize("sizes", [[4, 4, 4], [2, 3, 7], [1, 5, 6]])
def test_happy_and_unequal_commit_and_match_reference(sizes):
    res = run_scenario(Scenario(shard_sizes=sizes))
    assert res.verdict == Verdict.ACCEPTED
    assert res.commit is not None
    assert_generation_frozen_during_submission(res)
    assert res.commit.generation_after == 1
    refs = covered_reference(res)
    for p, (ref, n, workers) in refs.items():
        np.testing.assert_allclose(res.commit.gradients[p], ref,
                                   rtol=0, atol=1e-12)
        assert n == sum(sizes) and len(workers) == 3


def test_all_representative_completion_orders_give_identical_commit():
    base = run_scenario(Scenario(shard_sizes=[2, 3, 7])).commit
    for order in representative_orders(3, 2):
        res = run_scenario(Scenario(shard_sizes=[2, 3, 7], order=order))
        assert res.verdict == Verdict.ACCEPTED
        for p in base.gradients:
            np.testing.assert_array_equal(
                res.commit.gradients[p], base.gradients[p]
            )
        assert res.commit.generation_after == 1


def test_missing_gradient_has_explicit_placeholder_and_per_slot_denominator():
    res = run_scenario(Scenario(
        shard_sizes=[2, 3, 7], omit_params={"w2": {"w"}},
    ))
    assert res.verdict == Verdict.ACCEPTED
    refs = covered_reference(res)
    g_w, n_w, workers_w = refs["w"]
    np.testing.assert_allclose(res.commit.gradients["w"], g_w, atol=1e-12)
    assert n_w == 5 and workers_w == ["w0", "w1"]   # w2 被逐槽剔除
    g_b, n_b, workers_b = refs["b"]
    np.testing.assert_allclose(res.commit.gradients["b"], g_b, atol=1e-12)
    assert n_b == 12 and workers_b == ["w0", "w1", "w2"]
    # 桶归约依据显式记录不同分母。
    basis0 = res.commit.bucket_bases[0]
    dens = {s["param"]: s["weight_total"] for s in basis0["slots"]}
    assert dens == {"w": pytest.approx(5.0), "b": pytest.approx(12.0)}
    # 缺梯度不影响世代冻结保证。
    assert_generation_frozen_during_submission(res)


def test_worker_interruption_rejects_round_without_weight_update():
    res = run_scenario(Scenario(
        shard_sizes=[2, 3, 7],
        die_after={"w2": 0},
        silence_before_seal=6.0,
    ))
    assert res.verdict == Verdict.REJECTED_WORKER_LOST
    assert res.commit is None
    assert res.coordinator.generation == 0
    assert_generation_frozen_during_submission(res)
    rec = [d for d in res.coordinator.diagnostics().all()
           if d.verdict == Verdict.REJECTED_WORKER_LOST][-1]
    assert rec.key_state["lost_workers"] == ["w2"]
    assert rec.key_state["missing_buckets"] == [1]
    assert rec.key_state["staged_buckets"] == [0]  # 桶0曾收齐也不提交


def test_withheld_bucket_but_alive_is_indeterminate_then_resolvable():
    res = run_scenario(Scenario(
        shard_sizes=[2, 3, 7],
        withhold={("w2", 1)},
        silence_before_seal=1.0,
    ))
    assert res.verdict == Verdict.INDETERMINATE_PENDING
    assert res.commit is None
    assert res.coordinator.generation == 0
    # 轮仍开放：补发被压下的桶后可以封存（用 coordinator 直接补发）。
    coord = res.coordinator
    from gradbucket.graph import make_dataset, shard_indices
    from gradbucket.runtime import build_worker_payload
    x, y = make_dataset(12, seed=445)
    idx = shard_indices(12, 2, 3, shard_sizes=[2, 3, 7], seed=445)
    grads, n = LinearModel(ModelConfig()).gradients(x[idx], y[idx])
    vec, mask = build_worker_payload(res.layout, 1, grads, set())
    coord.heartbeat("w2")
    v = coord.submit_bucket(0, 0, "w2", 1, vec, mask, n)
    assert v == Verdict.ACCEPTED
    verdict, commit = coord.seal_round()
    assert verdict == Verdict.ACCEPTED
    assert commit.generation_after == 1


def test_interruption_before_any_submission_still_detected():
    res = run_scenario(Scenario(
        shard_sizes=[2, 3, 7],
        die_after={"w2": -1},  # 一次都不交
        silence_before_seal=6.0,
        order=[(w, b) for w in ["w0", "w1"] for b in range(2)],
    ))
    assert res.verdict == Verdict.REJECTED_WORKER_LOST
    assert res.commit is None


def test_rejected_and_indeterminate_are_distinct_categories_by_timeout():
    fresh = run_scenario(Scenario(
        shard_sizes=[2, 3, 7], withhold={("w2", 1)}, silence_before_seal=1.0,
    ))
    stale = run_scenario(Scenario(
        shard_sizes=[2, 3, 7], die_after={"w2": 0}, silence_before_seal=6.0,
    ))
    assert fresh.verdict == Verdict.INDETERMINATE_PENDING
    assert stale.verdict == Verdict.REJECTED_WORKER_LOST


def test_diagnostic_records_never_contain_raw_tensor_values():
    res = run_scenario(Scenario(shard_sizes=[2, 3, 7]))
    assert res.verdict == Verdict.ACCEPTED
    for d in res.coordinator.diagnostics().all():
        blob = str(d.key_state)
        # 原始梯度向量不出现在诊断文本中（仅有 l2_norm/shape 等摘要）。
        assert "vec" not in d.key_state
        for forbidden in ("array(", "ndarray"):
            assert forbidden not in blob
