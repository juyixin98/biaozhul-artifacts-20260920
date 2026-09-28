"""训练状态机测试：两阶段提交、世代、失联判定、端到端联合批对照。"""

from __future__ import annotations

import numpy as np
import pytest

from gradbucket.diagnostics import Verdict
from gradbucket.graph import LinearModel, ModelConfig, make_dataset, shard_indices
from gradbucket.reference import union_batch_gradient
from gradbucket.runtime import FakeClock, build_worker_payload
from gradbucket.tensors import build_layout
from gradbucket.training import RoundCoordinator

from conftest import explicit_weighted_average


def begin(coord, params=None, workers=("w0", "w1", "w2"), capacity=2,
          known_zero=("spare",)):
    model = LinearModel(ModelConfig())
    params = params or model.param_specs()
    initial = {p.name: p.zeros() for p in params}
    view = coord.begin_round(
        0, params, initial, list(workers),
        bucket_capacity=capacity, known_zero_params=known_zero,
    )
    return model, params, view


def worker_data(sizes=None):
    x, y = make_dataset(12, seed=445, n_in=3, n_out=2)
    sizes = sizes or [4, 4, 4]
    shards = []
    for r in range(3):
        idx = shard_indices(12, r, 3, shard_sizes=sizes, seed=445)
        shards.append((x[idx], y[idx]))
    return shards


def submit_all(coord, layout, shards, *, omit=None, order=None, workers=None):
    workers = workers or ["w0", "w1", "w2"]
    omit = omit or {}
    model = LinearModel(ModelConfig())
    order = order or [(w, b) for w in workers for b in range(layout.num_buckets)]
    for w, b in order:
        rank = int(w[1:])
        g, n = model.gradients(*shards[rank])
        vec, mask = build_worker_payload(layout, b, g, omit.get(w, set()))
        v = coord.submit_bucket(0, 0, w, b, vec, mask, n)
        assert v == Verdict.ACCEPTED


# ---------------------------------------------------------------------------
# 要求 3：桶完成不能提前更新导致后续梯度读取新权重
# ---------------------------------------------------------------------------


def test_staging_a_bucket_does_not_change_weights_or_generation(coordinator):
    model, params, view = begin(coordinator)
    layout = build_layout(0, params, 2)
    shards = worker_data()
    coordinator.heartbeat("w0"); coordinator.heartbeat("w1"); coordinator.heartbeat("w2")

    w_before = coordinator.snapshot_weights()
    g0, n0 = model.gradients(*shards[0])
    vec, mask = build_worker_payload(layout, 0, g0, set())
    coordinator.submit_bucket(0, 0, "w0", 0, vec, mask, n0)
    # 即便桶 0 三个人交齐并完成归约，权重与世代也必须纹丝不动。
    for w in ("w1", "w2"):
        g, n = model.gradients(*shards[int(w[1:])])
        v, m = build_worker_payload(layout, 0, g, set())
        coordinator.submit_bucket(0, 0, w, 0, v, m, n)
    assert coordinator.generation == 0
    for k in w_before:
        np.testing.assert_array_equal(
            coordinator.snapshot_weights()[k], w_before[k]
        )
    assert coordinator.round_view()["staged_buckets"] == [0]

    # 后提交的工作者读到的仍是旧权重：用旧权重算的梯度与服务端快照一致。
    snap = coordinator.snapshot_weights()
    assert np.all(snap["w"] == 0.0) and np.all(snap["b"] == 0.0)


def test_commit_updates_weights_atomically_and_bumps_generation(coordinator):
    model, params, _ = begin(coordinator)
    layout = build_layout(0, params, 2)
    shards = worker_data()
    for w in ("w0", "w1", "w2"):
        coordinator.heartbeat(w)
    submit_all(coordinator, layout, shards)
    assert coordinator.generation == 0
    verdict, commit = coordinator.seal_round(learning_rate=0.1)
    assert verdict == Verdict.ACCEPTED
    assert commit.generation_before == 0
    assert commit.generation_after == 1
    assert coordinator.generation == 1
    # 权重确实变了（非零梯度的 SGD 步）。
    assert np.any(np.abs(commit.weights["w"]) > 0)


def test_late_submission_after_seal_is_rejected(coordinator):
    model, params, _ = begin(coordinator)
    layout = build_layout(0, params, 2)
    shards = worker_data()
    for w in ("w0", "w1", "w2"):
        coordinator.heartbeat(w)
    submit_all(coordinator, layout, shards)
    coordinator.seal_round()
    g, n = model.gradients(*shards[0])
    vec, mask = build_worker_payload(layout, 0, g, set())
    v = coordinator.submit_bucket(0, 0, "w0", 0, vec, mask, n)
    assert v == Verdict.REJECTED_SEALED


def test_stale_generation_submission_is_rejected(coordinator):
    begin(coordinator)
    # 直接构造一个合法形状的提交，但世代号是上一代。
    vec = np.zeros(8)
    mask = np.ones(8, dtype=bool)
    coordinator.heartbeat("w0")
    v = coordinator.submit_bucket(0, 99, "w0", 0, vec, mask, 4)
    assert v == Verdict.REJECTED_STALE_ROUND


def test_wrong_round_submission_rejected(coordinator):
    begin(coordinator)
    coordinator.heartbeat("w0")
    v = coordinator.submit_bucket(5, 0, "w0", 0, np.zeros(8),
                                  np.ones(8, dtype=bool), 4)
    assert v == Verdict.REJECTED_STALE_ROUND


# ---------------------------------------------------------------------------
# 要求 4：工作者失联该轮拒绝；缺桶但存活 -> 无法判定
# ---------------------------------------------------------------------------


def test_seal_with_missing_bucket_but_fresh_workers_is_indeterminate(coordinator):
    _, params, _ = begin(coordinator)
    layout = build_layout(0, params, 2)
    shards = worker_data()
    for w in ("w0", "w1", "w2"):
        coordinator.heartbeat(w)
    # 只交桶 0。
    for w in ("w0", "w1", "w2"):
        g, n = LinearModel(ModelConfig()).gradients(*shards[int(w[1:])])
        vec, mask = build_worker_payload(layout, 0, g, set())
        coordinator.submit_bucket(0, 0, w, 0, vec, mask, n)
    # 时钟推进 1 秒（< 5 秒阈值），所有人续约。
    coordinator._clock.advance(1.0) if hasattr(coordinator, "_clock") else None
    verdict, commit = coordinator.seal_round()
    assert verdict == Verdict.INDETERMINATE_PENDING
    assert commit is None
    assert coordinator.generation == 0  # 无法判定时绝不能提交
    # 轮仍开放：补齐后可以成功封存。
    for w in ("w0", "w1", "w2"):
        coordinator.heartbeat(w)
        g, n = LinearModel(ModelConfig()).gradients(*shards[int(w[1:])])
        vec, mask = build_worker_payload(layout, 1, g, set())
        coordinator.submit_bucket(0, 0, w, 1, vec, mask, n)
    verdict, commit = coordinator.seal_round()
    assert verdict == Verdict.ACCEPTED


def test_seal_after_heartbeat_timeout_rejects_round_as_worker_lost(coordinator):
    _, params, _ = begin(coordinator)
    layout = build_layout(0, params, 2)
    shards = worker_data()
    for w in ("w0", "w1", "w2"):
        coordinator.heartbeat(w)
    # w0,w1 全部交齐；w2 只交桶 0，随后失联。
    for w in ("w0", "w1"):
        for b in range(2):
            g, n = LinearModel(ModelConfig()).gradients(*shards[int(w[1:])])
            vec, mask = build_worker_payload(layout, b, g, set())
            coordinator.submit_bucket(0, 0, w, b, vec, mask, n)
    g2, n2 = LinearModel(ModelConfig()).gradients(*shards[2])
    vec, mask = build_worker_payload(layout, 0, g2, set())
    coordinator.submit_bucket(0, 0, "w2", 0, vec, mask, n2)
    # 超过失联阈值；存活者续约，w2 不续约。
    coordinator._clock.advance(6.0)
    coordinator.heartbeat("w0"); coordinator.heartbeat("w1")
    verdict, commit = coordinator.seal_round()
    assert verdict == Verdict.REJECTED_WORKER_LOST
    assert commit is None
    assert coordinator.generation == 0
    view = coordinator.round_view()
    assert view["status"] == "REJECTED"


def test_rejected_round_cannot_be_resurrected(coordinator):
    _, params, _ = begin(coordinator)
    for w in ("w0", "w1", "w2"):
        coordinator.heartbeat(w)
    coordinator._clock.advance(9.0)
    verdict, _ = coordinator.seal_round()
    assert verdict == Verdict.REJECTED_WORKER_LOST
    # 迟到的工作者不能让被拒轮复活。
    v = coordinator.submit_bucket(0, 0, "w0", 0, np.zeros(8),
                                  np.ones(8, dtype=bool), 4)
    assert v == Verdict.REJECTED_SEALED


def test_worker_that_submitted_then_went_silent_is_detected(coordinator):
    # 提交本身刷新心跳；只有"最后一次接触"超时才判失联。
    _, params, _ = begin(coordinator)
    layout = build_layout(0, params, 2)
    shards = worker_data()
    for w in ("w0", "w1", "w2"):
        g, n = LinearModel(ModelConfig()).gradients(*shards[int(w[1:])])
        vec, mask = build_worker_payload(layout, 0, g, set())
        coordinator.submit_bucket(0, 0, w, 0, vec, mask, n)
    # 未超时时 seal：缺桶但刚接触过 -> INDETERMINATE，而不是 REJECT。
    coordinator._clock.advance(1.0)
    verdict, _ = coordinator.seal_round()
    assert verdict == Verdict.INDETERMINATE_PENDING
    # 超时后 seal：REJECTED_WORKER_LOST。
    coordinator._clock.advance(5.0)
    verdict, _ = coordinator.seal_round()
    assert verdict == Verdict.REJECTED_WORKER_LOST


# ---------------------------------------------------------------------------
# 要求 1/2：固定布局 + 不等批量端到端对照单进程联合批
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("sizes", [[4, 4, 4], [2, 3, 7], [1, 1, 10], [6, 1, 5]])
def test_end_to_end_matches_single_process_union_batch(coordinator, sizes):
    model = LinearModel(ModelConfig())
    params = model.param_specs()
    _, _, view = begin(coordinator, params=params)
    layout = build_layout(0, params, 2)
    shards = worker_data(sizes)
    for w in ("w0", "w1", "w2"):
        coordinator.heartbeat(w)
    submit_all(coordinator, layout, shards)
    verdict, commit = coordinator.seal_round(learning_rate=0.1)
    assert verdict == Verdict.ACCEPTED

    ref_grads, n_total = union_batch_gradient(LinearModel(ModelConfig()), shards)
    assert n_total == sum(sizes) == 12
    for name in ("w", "b", "spare"):
        np.testing.assert_allclose(
            commit.gradients[name], ref_grads[name], rtol=0, atol=1e-12,
            err_msg=f"分片 {sizes} 上 {name} 与联合批参照不符",
        )
    # 归约依据必须记录真实样本数，逐槽分母为总样本数。
    for basis in commit.bucket_bases:
        for slot in basis["slots"]:
            assert slot["weight_total"] == pytest.approx(float(sum(sizes)))
            assert slot["sample_counts"] == sizes
    # SGD 后权重 = 初值 - lr * 联合批梯度。
    for name in ("w", "b"):
        np.testing.assert_allclose(
            commit.weights[name], -0.1 * ref_grads[name], atol=1e-12
        )


def test_end_to_end_with_omitted_param_matches_covered_union(coordinator):
    model = LinearModel(ModelConfig())
    params = model.param_specs()
    begin(coordinator, params=params)
    layout = build_layout(0, params, 2)
    sizes = [2, 3, 7]
    shards = worker_data(sizes)
    for w in ("w0", "w1", "w2"):
        coordinator.heartbeat(w)
    # w2 没算 w（显式占位），其余参数照交。
    submit_all(coordinator, layout, shards, omit={"w2": {"w"}})
    verdict, commit = coordinator.seal_round()
    assert verdict == Verdict.ACCEPTED

    covered_grads, n_cov = union_batch_gradient(
        LinearModel(ModelConfig()), shards[:2]
    )
    full_grads, n_full = union_batch_gradient(
        LinearModel(ModelConfig()), shards
    )
    # w 槽只按 w0+w1 的 5 个样本平均。
    np.testing.assert_allclose(commit.gradients["w"], covered_grads["w"],
                               rtol=0, atol=1e-12)
    assert n_cov == 5
    # b 槽仍按全部 12 个样本平均。
    np.testing.assert_allclose(commit.gradients["b"], full_grads["b"],
                               rtol=0, atol=1e-12)
    assert n_full == 12
    # 归约依据逐槽记录不同分母。
    slot_w = commit.bucket_bases[0]["slots"][0]
    slot_b = commit.bucket_bases[0]["slots"][1]
    assert slot_w["sample_counts"] == [2, 3]
    assert slot_w["weight_total"] == pytest.approx(5.0)
    assert slot_b["sample_counts"] == [2, 3, 7]
    assert slot_b["weight_total"] == pytest.approx(12.0)


def test_no_evidence_when_buckets_complete_is_rejected_and_recoverable(coordinator):
    # 关掉 spare 的"已知零"豁免：所有人占位 w 槽 -> 零证据且非已知零。
    model = LinearModel(ModelConfig(include_spare=False))
    params = model.param_specs()  # 只有 w, b
    begin(coordinator, params=params, known_zero=())
    layout = build_layout(0, params, 2)
    shards = worker_data()
    for w in ("w0", "w1", "w2"):
        coordinator.heartbeat(w)
    # 桶0: w 全部占位，b 真实；桶1不存在（2个参数/容量2=1桶）。
    for w in ("w0", "w1", "w2"):
        g, n = LinearModel(ModelConfig(include_spare=False)).gradients(
            *shards[int(w[1:])]
        )
        vec, mask = build_worker_payload(layout, 0, g, {"w"})
        coordinator.submit_bucket(0, 0, w, 0, vec, mask, n)

    # 桶已齐 -> 无法补提，零证据是确定性终局：拒绝（而不是永久卡在暂时态）。
    verdict, commit = coordinator.seal_round()
    assert verdict == Verdict.REJECTED_NO_EVIDENCE
    assert commit is None
    assert coordinator.generation == 0
    assert coordinator.round_view()["status"] == "REJECTED"
    # 重复 seal 幂等返回同一终局类别，不抛异常。
    verdict2, commit2 = coordinator.seal_round()
    assert verdict2 == Verdict.REJECTED_NO_EVIDENCE and commit2 is None
    # 可恢复：reset 后能重新开一轮，不会被永久卡死。
    coordinator.reset_rejected_round()
    assert coordinator.round_view() is None
    begin(coordinator, params=params, known_zero=())


# ---------------------------------------------------------------------------
# 非法提交失败类别（经 coordinator 路径）
# ---------------------------------------------------------------------------


def test_bad_shape_submission_returns_stable_category(coordinator):
    begin(coordinator)
    coordinator.heartbeat("w0")
    v = coordinator.submit_bucket(
        0, 0, "w0", 0, np.zeros(7), np.ones(7, dtype=bool), 4
    )  # 桶0需要长度8
    assert v == Verdict.REJECTED_BUCKET_SHAPE


def test_partial_mask_submission_category(coordinator):
    begin(coordinator)
    coordinator.heartbeat("w0")
    mask = np.ones(8, dtype=bool)
    mask[3] = False
    v = coordinator.submit_bucket(0, 0, "w0", 0, np.zeros(8), mask, 4)
    assert v == Verdict.REJECTED_PARTIAL_SLOT_MASK


def test_zero_sample_count_category(coordinator):
    begin(coordinator)
    coordinator.heartbeat("w0")
    v = coordinator.submit_bucket(
        0, 0, "w0", 0, np.zeros(8), np.ones(8, dtype=bool), 0
    )
    assert v == Verdict.REJECTED_SAMPLE_COUNT


def test_duplicate_submission_category(coordinator):
    begin(coordinator)
    coordinator.heartbeat("w0")
    coordinator.submit_bucket(0, 0, "w0", 0, np.zeros(8),
                              np.ones(8, dtype=bool), 4)
    v = coordinator.submit_bucket(0, 0, "w0", 0, np.zeros(8),
                                  np.ones(8, dtype=bool), 4)
    assert v == Verdict.REJECTED_DUPLICATE


def test_duplicate_is_reported_even_when_second_payload_malformed(coordinator):
    # L3：重复判定优先于载荷校验，类别不被畸形的第二次提交干扰。
    begin(coordinator)
    coordinator.heartbeat("w0")
    coordinator.submit_bucket(0, 0, "w0", 0, np.zeros(8),
                              np.ones(8, dtype=bool), 4)
    v = coordinator.submit_bucket(0, 0, "w0", 0, np.zeros(3),
                                  np.ones(3, dtype=bool), 4)
    assert v == Verdict.REJECTED_DUPLICATE


def test_huge_sample_count_rejected_before_staging(coordinator):
    # H2：超大样本数必须在入暂存区前拒绝，绝不带着溢出分母封存。
    begin(coordinator)
    coordinator.heartbeat("w0")
    v = coordinator.submit_bucket(
        0, 0, "w0", 0, np.zeros(8), np.ones(8, dtype=bool), 10 ** 308
    )
    assert v == Verdict.REJECTED_SAMPLE_COUNT
    assert coordinator.round_view()["staged_buckets"] == []


def test_seal_is_idempotent_after_commit(coordinator):
    # M1：重复 seal 返回既有 commit，不抛异常、不二次推进世代。
    model, params, _ = begin(coordinator)
    layout = build_layout(0, params, 2)
    shards = worker_data()
    for w in ("w0", "w1", "w2"):
        coordinator.heartbeat(w)
    submit_all(coordinator, layout, shards)
    v1, c1 = coordinator.seal_round()
    assert v1 == Verdict.ACCEPTED
    v2, c2 = coordinator.seal_round()
    assert v2 == Verdict.ACCEPTED
    assert c1 is c2  # 返回缓存的同一结果
    assert coordinator.generation == 1


def test_seal_is_idempotent_after_reject(coordinator):
    begin(coordinator)
    for w in ("w0", "w1", "w2"):
        coordinator.heartbeat(w)
    coordinator._clock.advance(9.0)
    v1, c1 = coordinator.seal_round()
    assert v1 == Verdict.REJECTED_WORKER_LOST and c1 is None
    v2, c2 = coordinator.seal_round()
    assert v2 == Verdict.REJECTED_WORKER_LOST and c2 is None


def test_seal_without_round_raises_not_silent(coordinator):
    with pytest.raises(RuntimeError, match="没有轮次"):
        coordinator.seal_round()


def test_unknown_worker_rejected(coordinator):
    begin(coordinator)
    coordinator.heartbeat("w0")
    v = coordinator.submit_bucket(
        0, 0, "intruder", 0, np.zeros(8), np.ones(8, dtype=bool), 4
    )
    assert v == Verdict.REJECTED_STALE_ROUND


# ---------------------------------------------------------------------------
# 多轮世代
# ---------------------------------------------------------------------------


def test_two_consecutive_rounds_advance_generation(coordinator):
    model = LinearModel(ModelConfig())
    params = model.param_specs()
    begin(coordinator, params=params)
    layout = build_layout(0, params, 2)
    shards = worker_data()
    for w in ("w0", "w1", "w2"):
        coordinator.heartbeat(w)
    submit_all(coordinator, layout, shards)
    v1, c1 = coordinator.seal_round()
    assert v1 == Verdict.ACCEPTED and c1.generation_after == 1

    # 第二轮从上一代权重出发，使用新世代号。
    initial = coordinator.snapshot_weights()
    coordinator.begin_round(1, params, initial, ["w0", "w1", "w2"],
                            bucket_capacity=2, known_zero_params=["spare"])
    layout2 = build_layout(1, params, 2)
    for w in ("w0", "w1", "w2"):
        coordinator.heartbeat(w)
    # 工作者必须携带世代 1；携带世代 0 的迟到梯度被拒。
    stale = coordinator.submit_bucket(
        1, 0, "w0", 0, np.zeros(8), np.ones(8, dtype=bool), 4
    )
    assert stale == Verdict.REJECTED_STALE_ROUND
    model2 = LinearModel(ModelConfig())
    model2.set_weights(initial["w"], initial["b"])
    for w, b in [(x, y) for x in ("w0", "w1", "w2") for y in range(2)]:
        g, n = model2.gradients(*shards[int(w[1:])])
        vec, mask = build_worker_payload(layout2, b, g, set())
        assert coordinator.submit_bucket(1, 1, w, b, vec, mask, n) == \
            Verdict.ACCEPTED
    v2, c2 = coordinator.seal_round()
    assert v2 == Verdict.ACCEPTED
    assert (c2.generation_before, c2.generation_after) == (1, 2)


# ---------------------------------------------------------------------------
# 诊断：请求标识 + 关键状态 + 脱敏
# ---------------------------------------------------------------------------


def test_diagnostics_carry_request_ids_and_categories_without_tensor_values(
    coordinator,
):
    begin(coordinator)
    coordinator.heartbeat("w0", request_id="hb-1")
    coordinator.submit_bucket(
        0, 99, "w0", 0, np.zeros(8), np.ones(8, dtype=bool), 4,
        request_id="sub-stale",
    )
    records = {d.request_id: d for d in coordinator.diagnostics().all()}
    assert "sub-stale" in records
    stale = records["sub-stale"]
    assert stale.verdict == Verdict.REJECTED_STALE_ROUND
    assert stale.round_index == 0
    assert "99" in stale.reason and "0" in stale.reason
    # 任何记录的 key_state 都不得直接包含梯度向量（只允许指纹摘要）。
    for d in coordinator.diagnostics().all():
        text = str(d.key_state)
        assert "vec" not in d.key_state
        if "fingerprint" in d.key_state:
            fp = d.key_state["fingerprint"]
            assert set(fp) <= {"present", "shape", "finite", "l2_norm"}


def test_seal_diagnostic_explains_acceptance_basis(coordinator):
    model, params, _ = begin(coordinator)
    layout = build_layout(0, params, 2)
    shards = worker_data([2, 3, 7])
    for w in ("w0", "w1", "w2"):
        coordinator.heartbeat(w)
    submit_all(coordinator, layout, shards)
    coordinator.seal_round(request_id="seal-final")
    rec = [d for d in coordinator.diagnostics().for_request("seal-final")][0]
    assert rec.verdict == Verdict.ACCEPTED
    assert rec.key_state["generation_after"] == 1
    # 归约依据在诊断里，且只含指纹不含张量值。
    assert len(rec.key_state["bucket_bases"]) == 2
    for basis in rec.key_state["bucket_bases"]:
        for slot in basis["slots"]:
            assert "result" in slot and "l2_norm" in slot["result"]


# ---------------------------------------------------------------------------
# M4：开工宽限——首次接触前不把"启动慢"误判成"节点失联"
# ---------------------------------------------------------------------------


def _coord_with_grace(timeout, grace):
    from gradbucket.runtime import FakeClock
    from gradbucket.diagnostics import DiagnosticLog
    return RoundCoordinator(
        liveness_timeout=timeout, startup_grace=grace,
        clock=FakeClock(), diaglog=DiagnosticLog(),
    )


def test_seal_before_first_contact_within_grace_is_indeterminate():
    coord = _coord_with_grace(timeout=5.0, grace=5.0)
    begin(coord)
    # 无人任何心跳/提交，时钟只推进 1 秒（在开工宽限内）。
    coord._clock.advance(1.0)
    verdict, commit = coord.seal_round()
    assert verdict == Verdict.INDETERMINATE_PENDING
    assert commit is None
    assert coord.round_view()["status"] == "OPEN"  # 未被永久判死


def test_seal_before_first_contact_beyond_grace_is_rejected():
    coord = _coord_with_grace(timeout=5.0, grace=2.0)
    begin(coord)
    coord._clock.advance(3.0)  # 超过开工宽限仍无人报到
    verdict, _ = coord.seal_round()
    assert verdict == Verdict.REJECTED_WORKER_LOST


def test_slow_starting_worker_can_join_within_grace_then_round_commits():
    # 宽限期内慢启动工作者完成提交，轮仍可正常封存——不会因开局无心跳而死。
    coord = _coord_with_grace(timeout=5.0, grace=5.0)
    model, params, _ = begin(coord)
    layout = build_layout(0, params, 2)
    shards = worker_data()
    # 不做开工心跳，直接在时钟推进 1 秒后由各工作者提交（提交即首次接触）。
    coord._clock.advance(1.0)
    submit_all(coord, layout, shards)
    verdict, commit = coord.seal_round()
    assert verdict == Verdict.ACCEPTED
    assert commit is not None


def test_overflow_during_staging_rejects_round_without_nan_weights(coordinator):
    # 样本数合法（≤2^53），但梯度极大导致 n*g 累加溢出：
    # 桶收齐做暂存归约时即终结本轮，绝不封存 NaN 权重。
    begin(coordinator)
    big = 2 ** 53
    for w in ("w0", "w1", "w2"):
        coordinator.heartbeat(w)
    # 桶0 = w(6)+b(2)，全部放 1e300
    for w in ("w0", "w1", "w2"):
        v = coordinator.submit_bucket(
            0, 0, w, 0, np.full(8, 1e300), np.ones(8, dtype=bool), big
        )
    last_v = v
    assert last_v == Verdict.REJECTED_NON_FINITE
    assert coordinator.round_view()["status"] == "REJECTED"
    assert coordinator.generation == 0
    for arr in coordinator.snapshot_weights().values():
        assert np.all(np.isfinite(arr))
