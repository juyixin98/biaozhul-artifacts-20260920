"""HTTP 接口：权限隔离、错误结构、可解释追踪、分配隐藏。"""
from __future__ import annotations

import base64
import json
import sqlite3

INV = {"Authorization": "Bearer synt-investigator-token"}
AUD = {"Authorization": "Bearer synt-auditor-token"}
ADM = {"Authorization": "Bearer synt-admin-token"}
SEED_B64 = base64.b64encode(b"rct-fixed-seed-v1-A".ljust(32, b"0")).decode()


def _create_study(client, study_id="API-STUDY", tail="keep_open"):
    c, _, _ = client
    r = c.post("/v1/studies", headers=INV, json={
        "study_id": study_id,
        "arms": [{"arm_id": "control", "ratio": 1},
                 {"arm_id": "treatment", "ratio": 1}],
        "factors": [{"name": "center", "levels": ["C1", "C2"]},
                    {"name": "stage", "levels": ["early", "late"]}],
        "block_multiple": 2,
        "tail_policy": tail,
        "seed_base64": SEED_B64,
    })
    assert r.status_code == 201, r.text
    return r.json()["data"]


def test_healthz_anonymous_ok(client):
    c, _, _ = client
    r = c.get("/healthz")
    assert r.status_code == 200
    assert "request_id" in r.json()


def test_missing_and_bad_token_categories(client):
    c, _, _ = client
    r = c.post("/v1/studies", json={})
    assert r.status_code == 401
    assert r.json()["error"]["category"] == "MISSING_CREDENTIALS"
    r = c.post("/v1/studies", headers={"Authorization": "Bearer nope"},
               json={})
    assert r.status_code == 401
    assert r.json()["error"]["category"] == "INVALID_TOKEN"


def test_permission_matrix(client):
    c, _, _ = client
    _create_study(client)
    body = {"subject_id": "X1",
            "features": {"center": "C1", "stage": "early"}}
    # auditor 不能登记
    r = c.post("/v1/studies/API-STUDY/allocations", headers=AUD, json=body)
    assert r.status_code == 403
    assert r.json()["error"]["category"] == "FORBIDDEN_ROLE"
    # admin 也不能登记
    r = c.post("/v1/studies/API-STUDY/allocations", headers=ADM, json=body)
    assert r.status_code == 403
    # investigator 可以登记
    r = c.post("/v1/studies/API-STUDY/allocations", headers=INV, json=body)
    assert r.status_code == 200
    # auditor 不能揭盲单对象
    r = c.get("/v1/studies/API-STUDY/assignments/X1", headers=AUD)
    assert r.status_code == 403
    # auditor 可读证据
    r = c.get("/v1/studies/API-STUDY/evidence/balance", headers=AUD)
    assert r.status_code == 200
    # investigator 不能做流复核
    r = c.post("/v1/studies/API-STUDY/evidence/verify", headers=INV)
    assert r.status_code == 403
    # auditor 可以
    r = c.post("/v1/studies/API-STUDY/evidence/verify", headers=AUD)
    assert r.status_code == 200 and r.json()["data"]["verdict"] == "PASS"


def test_allocate_response_has_explained_trace(client):
    c, _, _ = client
    _create_study(client)
    r = c.post("/v1/studies/API-STUDY/allocations", headers=INV,
               json={"subject_id": "X1",
                     "features": {"center": "C1", "stage": "early"}})
    body = r.json()
    data, trace = body["data"], body["trace"]
    assert data["arm"] in ("control", "treatment")
    assert trace["request_id"]
    assert r.headers["X-Request-ID"] == trace["request_id"]
    steps = [s["step"] for s in trace["steps"]]
    assert "load_contract" in steps and "claim_block_slot_in_serial_tx" in steps
    assert trace["versions"]["contract_spec"] == "contract/v1"
    assert trace["random_sources"][0]["prf"] == "HMAC-SHA256"
    assert trace["failure"] is None


def test_replay_marks_outcome_and_uncertainty(client):
    c, _, _ = client
    _create_study(client)
    payload = {"subject_id": "X1",
               "features": {"center": "C1", "stage": "early"}}
    r1 = c.post("/v1/studies/API-STUDY/allocations", headers=INV, json=payload)
    r2 = c.post("/v1/studies/API-STUDY/allocations", headers=INV, json=payload)
    assert r1.json()["data"]["outcome"] == "committed"
    assert r2.json()["data"]["outcome"] == "replayed"
    assert r2.json()["data"]["arm"] == r1.json()["data"]["arm"]
    cats = [u["category"] for u in r2.json()["trace"]["uncertainties"]]
    assert "IDEMPOTENT_REPLAY" in cats


def test_feature_tamper_error_is_separate_and_explained(client):
    c, _, _ = client
    _create_study(client)
    c.post("/v1/studies/API-STUDY/allocations", headers=INV,
           json={"subject_id": "X1",
                 "features": {"center": "C1", "stage": "early"}})
    r = c.post("/v1/studies/API-STUDY/allocations", headers=INV,
               json={"subject_id": "X1",
                     "features": {"center": "C2", "stage": "early"}})
    assert r.status_code == 409
    err = r.json()["error"]
    assert err["category"] == "FEATURES_CHANGED_AFTER_ALLOCATION"
    assert "digest" in json.dumps(err["details"])
    # 原分配仍可查
    got = c.get("/v1/studies/API-STUDY/assignments/X1", headers=INV)
    assert got.status_code == 200


def test_validation_error_category_on_bad_features(client):
    c, _, _ = client
    _create_study(client)
    r = c.post("/v1/studies/API-STUDY/allocations", headers=INV,
               json={"subject_id": "X2",
                     "features": {"center": "NOPE", "stage": "early"}})
    assert r.status_code == 422
    assert r.json()["error"]["category"] in (
        "NON_DISCRETE_STRATUM_VALUE", "REQUEST_VALIDATION_FAILED")


def test_seed_is_never_returned(client):
    c, _, _ = client
    created = _create_study(client)
    text = json.dumps(created)
    assert SEED_B64 not in text
    assert "seed" not in json.dumps(created).lower() or \
        "seed_fingerprint" in text  # 只允许出现指纹
    r = c.get("/v1/studies/API-STUDY/audit", headers=AUD)
    assert SEED_B64 not in r.text


def test_open_block_permutation_not_persisted(client):
    """分配隐藏：未满区组的完整置换不能落库，库读者无法预知未来次序。"""
    c, _, tmp = client
    _create_study(client)
    # 只登记 2/4
    for i in range(2):
        c.post("/v1/studies/API-STUDY/allocations", headers=INV,
               json={"subject_id": f"H{i}",
                     "features": {"center": "C1", "stage": "early"}})
    db_path = str(tmp / "api.db")
    with sqlite3.connect(db_path) as raw:
        row = raw.execute(
            "SELECT perm_json, filled, status FROM blocks"
        ).fetchone()
    perm_json, filled, status = row
    assert filled == 2 and status == "open"
    assert perm_json is None  # 未来次序未落盘


def test_balance_and_verify_and_audit(client):
    c, _, _ = client
    _create_study(client)
    for i in range(5):  # 奇数 → 有尾组
        c.post("/v1/studies/API-STUDY/allocations", headers=INV,
               json={"subject_id": f"B{i}",
                     "features": {"center": "C1", "stage": "early"}})
    bal = c.get("/v1/studies/API-STUDY/evidence/balance",
                headers=AUD).json()["data"]
    assert bal["verdict"] == "PASS"
    assert len(bal["uncertainties"]) == 1
    assert bal["uncertainties"][0]["category"] == "INCOMPLETE_TAIL_BLOCK"
    assert bal["blocks"][0]["counts"]["control"] == 2
    assert bal["blocks"][0]["counts"]["treatment"] == 2

    ver = c.post("/v1/studies/API-STUDY/evidence/verify",
                 headers=AUD).json()["data"]
    assert ver["verdict"] == "PASS"
    assert ver["n_allocations"] == 5

    aud = c.get("/v1/studies/API-STUDY/audit", headers=AUD).json()["data"]
    actions = {e["action"] for e in aud["events"]}
    assert "allocate" in actions
    # 审计事件带 request_id 与契约指纹
    ev = next(e for e in aud["events"] if e["action"] == "allocate"
              and e["outcome"] == "committed")
    assert ev["request_id"] and ev["contract_fingerprint"].startswith("ctr_")
    assert ev["stream_locators"]  # 记录了随机流坐标


def test_seal_study_and_open_block_endpoints(client):
    c, _, _ = client
    _create_study(client, tail="seal_early")
    # 非 admin 不能开组
    r = c.post("/v1/studies/API-STUDY/blocks", headers=INV,
               json={"stratum_key": "C1|early"})
    assert r.status_code == 403
    # admin 开组
    r = c.post("/v1/studies/API-STUDY/blocks", headers=ADM,
               json={"stratum_key": "C1|early"})
    assert r.status_code == 200
    assert r.json()["data"]["block_index"] == 0
    # 非法层键 → 422 稳定类别
    r = c.post("/v1/studies/API-STUDY/blocks", headers=ADM,
               json={"stratum_key": "NOPE|early"})
    assert r.status_code == 422
    assert r.json()["error"]["category"] == "NON_DISCRETE_STRATUM_VALUE"
    # 请求体缺字段 → 统一校验信封
    r = c.post("/v1/studies/API-STUDY/blocks", headers=ADM, json={})
    assert r.status_code == 422
    assert r.json()["error"]["category"] == "REQUEST_VALIDATION_FAILED"

    # 登记一个后整体封研究，再登记被拒
    c.post("/v1/studies/API-STUDY/allocations", headers=INV,
           json={"subject_id": "Z1",
                 "features": {"center": "C1", "stage": "early"}})
    r = c.post("/v1/studies/API-STUDY/actions/seal", headers=ADM)
    assert r.status_code == 200 and r.json()["data"]["sealed"] is True
    r = c.post("/v1/studies/API-STUDY/allocations", headers=INV,
               json={"subject_id": "Z2",
                     "features": {"center": "C1", "stage": "early"}})
    assert r.status_code == 409
    assert r.json()["error"]["category"] == "ENROLLMENT_CLOSED"


def test_seal_tails_admin_only_and_trace_uncertainty(client):
    c, _, _ = client
    _create_study(client, tail="keep_open")
    c.post("/v1/studies/API-STUDY/allocations", headers=INV,
           json={"subject_id": "S0",
                 "features": {"center": "C1", "stage": "early"}})
    r = c.post("/v1/studies/API-STUDY/actions/seal-tails", headers=INV)
    assert r.status_code == 403
    r = c.post("/v1/studies/API-STUDY/actions/seal-tails", headers=ADM)
    assert r.status_code == 200
    uc = r.json()["trace"]["uncertainties"]
    assert any(u["category"] == "TAIL_SEALED_INCOMPLETE" for u in uc)


def test_distribution_diagnostic_non_proof_language(client):
    c, _, _ = client
    r = c.post("/v1/diagnostics/distribution", headers=AUD, json={
        "block_size": 4,
        "arms": [{"arm_id": "control", "ratio": 1},
                 {"arm_id": "treatment", "ratio": 1}],
        "blocks_per_seed": 200,
    })
    assert r.status_code == 200, r.text
    data = r.json()["data"]["results"]
    enum_res = data["permutation_enumeration"]
    assert enum_res["possible_permutations"] == 24
    assert enum_res["departure_detected"] is False
    pos_res = data["arm_frequency_by_position"]
    assert pos_res["departure_detected"] is False
    # 非证明声明必须显式出现在不确定结论中
    cats = [u["category"] for u in r.json()["trace"]["uncertainties"]]
    assert "STATISTICAL_NON_PROOF" in cats


def test_audit_log_jsonl_links_request_id(client):
    c, _, tmp = client
    _create_study(client)
    c.post("/v1/studies/API-STUDY/allocations", headers=INV,
           json={"subject_id": "L1",
                 "features": {"center": "C1", "stage": "early"}})
    lines = (tmp / "audit.log").read_text(encoding="utf-8").strip().splitlines()
    entries = [json.loads(x) for x in lines]
    alloc = [e for e in entries if e.get("action") == "allocate"]
    assert alloc and alloc[0]["request_id"]
    assert alloc[0]["outcome"] in ("committed", "replayed")
    assert alloc[0]["spec_version"] == "contract/v1"
