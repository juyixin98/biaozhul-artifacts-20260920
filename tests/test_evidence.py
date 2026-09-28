"""均衡核算与流复核的判定逻辑（含人为篡改必须 FAIL）。"""
from __future__ import annotations

from app.evidence.balance import balance_report
from app.evidence.stream_verify import verify_study
from tests.conftest import make_two_arm_study


def _rows(service, study_id):
    with service.db.connection() as conn:
        loaded = service.load_contract(conn, study_id)
        blocks = service.repo.list_blocks(conn, study_id)
        allocations = service.repo.list_allocations(conn, study_id)
    return loaded["contract"], blocks, allocations


def test_balance_full_blocks_exact_and_tail_uncertain(runtime):
    svc = runtime["service"]
    make_two_arm_study(svc)
    for i in range(5):  # 4 满 + 1 尾
        svc.allocate(
            study_id="TV-STUDY", subject_id=f"Q{i}",
            features={"center": "C1", "stage": "early"},
            idempotency_key=None, actor_role="investigator",
            request_id=f"q{i}")
    contract, blocks, allocations = _rows(svc, "TV-STUDY")
    report = balance_report(contract, blocks, allocations)
    assert report["verdict"] == "PASS"
    full = [b for b in report["blocks"] if b["filled"] == b["block_size"]]
    tail = [b for b in report["blocks"] if b["filled"] != b["block_size"]]
    assert len(full) == 1 and len(tail) == 1
    assert full[0]["deviations"] == {"control": 0, "treatment": 0}
    assert tail[0]["missing_to_completion"] == 3
    assert len(report["uncertainties"]) == 1
    assert report["failures"] == []


def test_balance_detects_violation_if_storage_corrupted(runtime):
    """模拟存储被外部破坏：完整区组比例不符 → 必须 FAIL（不是不确定）。"""
    svc = runtime["service"]
    make_two_arm_study(svc)
    for i in range(4):
        svc.allocate(
            study_id="TV-STUDY", subject_id=f"Q{i}",
            features={"center": "C1", "stage": "early"},
            idempotency_key=None, actor_role="investigator",
            request_id=f"q{i}")
    # 直接改库，把一条 allocation 的臂改成错误值
    with svc.db.write_tx() as conn:
        conn.execute("UPDATE allocations SET arm='control' WHERE id="
                     "(SELECT MIN(id) FROM allocations)")
    contract, blocks, allocations = _rows(svc, "TV-STUDY")
    report = balance_report(contract, blocks, allocations)
    assert report["verdict"] == "FAIL"
    assert report["failures"][0]["category"] == "BLOCK_RATIO_VIOLATION"


def test_stream_verify_pass_on_clean_study(runtime):
    svc = runtime["service"]
    make_two_arm_study(svc)
    for i in range(6):
        svc.allocate(
            study_id="TV-STUDY", subject_id=f"V{i}",
            features={"center": "C1", "stage": "early"},
            idempotency_key=None, actor_role="investigator",
            request_id=f"v{i}")
    out = verify_study(svc.db, svc.vault, "TV-STUDY", svc.repo)
    assert out["verdict"] == "PASS"
    assert out["n_allocations"] == 6
    # 种子证明与指纹都核对
    check_names = {c["check"] for c in out["checks"]}
    assert {"seed_proof", "seed_fingerprint",
            "allocation_recompute"} <= check_names


def test_stream_verify_fails_on_tampered_arm(runtime):
    svc = runtime["service"]
    make_two_arm_study(svc)
    for i in range(4):
        svc.allocate(
            study_id="TV-STUDY", subject_id=f"V{i}",
            features={"center": "C1", "stage": "early"},
            idempotency_key=None, actor_role="investigator",
            request_id=f"v{i}")
    # 直接改库：位置 0 的真实臂为 treatment（见冻结向量），改成 control
    with svc.db.write_tx() as conn:
        conn.execute(
            "UPDATE allocations SET arm='control' "
            "WHERE id=(SELECT MIN(id) FROM allocations)")
    out = verify_study(svc.db, svc.vault, "TV-STUDY", svc.repo)
    assert out["verdict"] == "FAIL"
    assert any(f["category"] == "VERIFICATION_FAILED"
               for f in out["failures"])


def test_stream_verify_uncertain_without_seed(runtime, tmp_path):
    """保险库缺少种子 → UNCERTAIN（证据不足），绝不是 PASS。"""
    from app.repository_support import make_service
    svc = runtime["service"]
    make_two_arm_study(svc)
    svc.allocate(
        study_id="TV-STUDY", subject_id="V0",
        features={"center": "C1", "stage": "early"},
        idempotency_key=None, actor_role="investigator", request_id="v0")
    # 用一个空保险库
    empty_vault = make_service(
        db_path=str(tmp_path / "other.db"),
        seeds_path=str(tmp_path / "empty-seeds.json"))["vault"]
    out = verify_study(svc.db, empty_vault, "TV-STUDY", svc.repo)
    assert out["verdict"] == "UNCERTAIN"
    assert out["category"] == "RECOVERY_SEED_MISSING"
