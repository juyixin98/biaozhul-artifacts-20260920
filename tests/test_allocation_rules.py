"""幂等、身份冻结、特征篡改、尾组策略、恢复。"""
from __future__ import annotations

import concurrent.futures

import pytest

from app.core.seed import SecretSeed
from app.errors import AppError, ErrorCategory
from tests.conftest import SEED_A_B64, make_two_arm_study


def test_repeated_request_returns_same_allocation(runtime):
    svc = runtime["service"]
    make_two_arm_study(svc)
    kw = dict(study_id="TV-STUDY", subject_id="P1",
              features={"center": "C1", "stage": "early"},
              idempotency_key=None, actor_role="investigator")
    first = svc.allocate(request_id="r1", **kw)
    second = svc.allocate(request_id="r2", **kw)
    assert first["outcome"] == "committed"
    assert second["outcome"] == "replayed"
    assert second["arm"] == first["arm"]
    assert second["block_index"] == first["block_index"]
    assert second["position"] == first["position"]
    assert second["created_at"] == first["created_at"]
    assert second["original_request_id"] == "r1"


def test_feature_change_after_allocation_rejected_and_original_kept(runtime):
    svc = runtime["service"]
    make_two_arm_study(svc)
    first = svc.allocate(
        study_id="TV-STUDY", subject_id="P1",
        features={"center": "C1", "stage": "early"},
        idempotency_key=None, actor_role="investigator", request_id="r1")
    # 改变分层特征 → 明确失败类别
    with pytest.raises(AppError) as ei:
        svc.allocate(
            study_id="TV-STUDY", subject_id="P1",
            features={"center": "C2", "stage": "early"},
            idempotency_key=None, actor_role="investigator", request_id="r2")
    assert ei.value.category == ErrorCategory.FEATURES_CHANGED_AFTER_ALLOCATION
    # 改回原特征：原分配置之不动
    again = svc.allocate(
        study_id="TV-STUDY", subject_id="P1",
        features={"center": "C1", "stage": "early"},
        idempotency_key=None, actor_role="investigator", request_id="r3")
    assert again["outcome"] == "replayed"
    assert again["arm"] == first["arm"]
    # 库里只有一条分配
    with svc.db.connection() as conn:
        rows = svc.repo.list_allocations(conn, "TV-STUDY")
    assert len(rows) == 1


def test_different_subjects_same_stratum_get_different_slots(runtime):
    svc = runtime["service"]
    make_two_arm_study(svc)
    arms = []
    for i in range(4):
        out = svc.allocate(
            study_id="TV-STUDY", subject_id=f"S{i}",
            features={"center": "C1", "stage": "early"},
            idempotency_key=None, actor_role="investigator",
            request_id=f"r{i}")
        arms.append(out)
    positions = {(a["block_index"], a["position"]) for a in arms}
    assert len(positions) == 4
    assert sorted(a["arm"] for a in arms) == ["control", "control",
                                              "treatment", "treatment"]


def test_idempotency_key_reuse_by_other_subject_rejected(runtime):
    svc = runtime["service"]
    make_two_arm_study(svc)
    svc.allocate(
        study_id="TV-STUDY", subject_id="A1",
        features={"center": "C1", "stage": "early"},
        idempotency_key="key-1", actor_role="investigator", request_id="r1")
    with pytest.raises(AppError) as ei:
        svc.allocate(
            study_id="TV-STUDY", subject_id="A2",
            features={"center": "C1", "stage": "early"},
            idempotency_key="key-1", actor_role="investigator", request_id="r2")
    assert ei.value.category == ErrorCategory.IDEMPOTENCY_KEY_REUSE_CONFLICT


def test_keep_open_tail_continues_same_block_then_new_block(runtime):
    svc = runtime["service"]
    make_two_arm_study(svc)
    outs = []
    for i in range(6):  # 区组长 4 → 4 满 + 2 进尾组
        outs.append(svc.allocate(
            study_id="TV-STUDY", subject_id=f"T{i}",
            features={"center": "C1", "stage": "early"},
            idempotency_key=None, actor_role="investigator",
            request_id=f"r{i}"))
    assert outs[3]["block_became_full"] is True
    assert outs[4]["block_index"] == 1 and outs[4]["position"] == 0
    assert outs[5]["block_index"] == 1 and outs[5]["position"] == 1


def test_seal_early_wave_enrollment(runtime):
    svc = runtime["service"]
    make_two_arm_study(svc, tail="seal_early")
    # 未开组直接登记 → 拒绝
    with pytest.raises(AppError) as ei:
        svc.allocate(
            study_id="TV-STUDY", subject_id="W0",
            features={"center": "C1", "stage": "early"},
            idempotency_key=None, actor_role="investigator", request_id="r0")
    assert ei.value.category == ErrorCategory.STRATUM_SEALED
    # 管理员开组
    opened = svc.open_next_block(
        study_id="TV-STUDY", stratum_key="C1|early",
        actor_role="admin", request_id="open1")
    assert opened["block_index"] == 0
    for i in range(3):  # 只填 3 个，留 1 个尾组空位
        svc.allocate(
            study_id="TV-STUDY", subject_id=f"W{i}",
            features={"center": "C1", "stage": "early"},
            idempotency_key=None, actor_role="investigator",
            request_id=f"r{i}")
    # 重复开组被拒绝
    with pytest.raises(AppError) as ei:
        svc.open_next_block(
            study_id="TV-STUDY", stratum_key="C1|early",
            actor_role="admin", request_id="open-dup")
    assert ei.value.category == ErrorCategory.OPEN_TAIL_BLOCK
    # 封尾：尾组未满即封闭
    sealed = svc.seal_tails(study_id="TV-STUDY", actor_role="admin",
                            request_id="seal1")
    assert sealed["sealed_tails"][0]["missing"] == 1
    # 再登记 → 拒绝（不能偷偷补开）
    with pytest.raises(AppError) as ei:
        svc.allocate(
            study_id="TV-STUDY", subject_id="W9",
            features={"center": "C1", "stage": "early"},
            idempotency_key=None, actor_role="investigator", request_id="r9")
    assert ei.value.category == ErrorCategory.STRATUM_SEALED
    # 重开后可继续，且 block_index 接续为 1
    svc.open_next_block(study_id="TV-STUDY", stratum_key="C1|early",
                        actor_role="admin", request_id="open2")
    cont = svc.allocate(
        study_id="TV-STUDY", subject_id="W10",
        features={"center": "C1", "stage": "early"},
        idempotency_key=None, actor_role="investigator", request_id="r10")
    assert cont["block_index"] == 1 and cont["position"] == 0


def test_concurrent_enrollment_no_slot_collision(runtime):
    svc = runtime["service"]
    make_two_arm_study(svc)

    def go(i):
        return svc.allocate(
            study_id="TV-STUDY", subject_id=f"C{i:02d}",
            features={"center": "C1", "stage": "early"},
            idempotency_key=None, actor_role="investigator",
            request_id=f"c{i}")

    errors = []
    results = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        futs = [pool.submit(go, i) for i in range(24)]  # 6 个整组
        for f in concurrent.futures.as_completed(futs):
            try:
                results.append(f.result())
            except AppError as e:
                errors.append(e)
    assert not errors
    slots = {(r["stratum_key"], r["block_index"], r["position"])
             for r in results}
    assert len(slots) == 24
    assert len({r["subject_id"] for r in results}) == 24
    counts = {}
    for r in results:
        counts[r["arm"]] = counts.get(r["arm"], 0) + 1
    assert counts == {"control": 12, "treatment": 12}


def test_restart_replays_and_continues(tmp_path):
    from app.repository_support import make_service
    db, seeds = str(tmp_path / "x.db"), str(tmp_path / "x-seeds.json")
    rt1 = make_service(db_path=db, seeds_path=seeds,
                       log_path=str(tmp_path / "x.log"))
    make_two_arm_study(rt1["service"])
    first = rt1["service"].allocate(
        study_id="TV-STUDY", subject_id="K1",
        features={"center": "C1", "stage": "early"},
        idempotency_key=None, actor_role="investigator", request_id="k1")
    del rt1  # 模拟进程退出（文件已落盘）

    rt2 = make_service(db_path=db, seeds_path=seeds,
                       log_path=str(tmp_path / "x2.log"))
    replay = rt2["service"].allocate(
        study_id="TV-STUDY", subject_id="K1",
        features={"center": "C1", "stage": "early"},
        idempotency_key=None, actor_role="investigator", request_id="k1b")
    assert replay["outcome"] == "replayed"
    assert replay["arm"] == first["arm"]
    cont = rt2["service"].allocate(
        study_id="TV-STUDY", subject_id="K2",
        features={"center": "C1", "stage": "early"},
        idempotency_key=None, actor_role="investigator", request_id="k2")
    assert cont["outcome"] == "committed" and cont["position"] == 1


def test_seed_is_required_and_withheld():
    with pytest.raises(AppError) as ei:
        SecretSeed.from_base64("")
    assert ei.value.category == ErrorCategory.SEED_REQUIRED
    seed = SecretSeed.from_base64(SEED_A_B64)
    assert "withheld" in repr(seed)
    assert SEED_A_B64 not in repr(seed)


def test_unknown_study_and_subject_categories(runtime):
    svc = runtime["service"]
    with pytest.raises(AppError) as ei:
        svc.allocate(
            study_id="GHOST", subject_id="X",
            features={"center": "C1", "stage": "early"},
            idempotency_key=None, actor_role="investigator", request_id="g")
    assert ei.value.category == ErrorCategory.UNKNOWN_STUDY
    make_two_arm_study(svc)
    with pytest.raises(AppError) as ei:
        svc.get_assignment(study_id="TV-STUDY", subject_id="NOBODY",
                           actor_role="investigator", request_id="n")
    assert ei.value.category == ErrorCategory.SUBJECT_NOT_FOUND


def test_duplicate_study_creation_rejected(runtime):
    svc = runtime["service"]
    make_two_arm_study(svc)
    with pytest.raises(AppError) as ei:
        make_two_arm_study(svc)
    assert ei.value.category == ErrorCategory.STUDY_ALREADY_EXISTS


def test_same_subject_different_idempotency_key_rejected(runtime):
    svc = runtime["service"]
    make_two_arm_study(svc)
    svc.allocate(
        study_id="TV-STUDY", subject_id="P9",
        features={"center": "C1", "stage": "early"},
        idempotency_key="orig-key", actor_role="investigator", request_id="a")
    with pytest.raises(AppError) as ei:
        svc.allocate(
            study_id="TV-STUDY", subject_id="P9",
            features={"center": "C1", "stage": "early"},
            idempotency_key="other-key", actor_role="investigator",
            request_id="b")
    assert ei.value.category == ErrorCategory.IDEMPOTENCY_KEY_MISMATCH


def test_seed_proof_roundtrip():
    seed = SecretSeed.from_base64(SEED_A_B64)
    proof = seed.proof(iterations=10_000)
    assert seed.verify_proof(proof) is True
    other = SecretSeed.generate()
    assert other.verify_proof(proof) is False
