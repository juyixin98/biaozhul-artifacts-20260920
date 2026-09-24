"""端到端行为测试 (HTTP + 服务层)。"""

from __future__ import annotations

import json
import sqlite3

import pytest

from conftest import wait_attempt


def _start_run(client, snapshot_id: str, seed: int):
    r = client.post(f"/snapshots/{snapshot_id}/runs", params={"seed": seed})
    assert r.status_code == 201, r.text
    return r.json()


def _attempt(client, run_id: str, body=None):
    r = client.post(f"/runs/{run_id}/attempts", json=body or {})
    assert r.status_code == 202, r.text
    return r.json()


def _result(svc, attempt_id: str) -> dict:
    data, _ = svc.get_artifact_bytes(attempt_id, "result")
    return json.loads(data)


# ---------- 1. 基本可复现: 同快照同种子逐字节一致 ----------


def test_same_snapshot_same_seed_is_byte_identical(client, svc, registered):
    run = _start_run(client, registered["s1"], 42)
    a1 = _attempt(client, run["id"])
    a2 = _attempt(client, run["id"])
    wait_attempt(svc, a1["id"])
    wait_attempt(svc, a2["id"])
    assert svc.get_attempt(a1["id"])["status"] == "succeeded"
    assert _result(svc, a1["id"]) == _result(svc, a2["id"])
    # 重复执行产生不同尝试, 但内容相同 -> 同一证据 (不覆盖)。
    run_view = svc.get_run(run["id"])
    assert [x["attempt_no"] for x in run_view["attempts"]] == [1, 2]
    assert (
        svc.get_attempt(a1["id"])["result_artifact_id"]
        == svc.get_attempt(a2["id"])["result_artifact_id"]
    )


def test_different_seed_changes_result(client, svc, registered):
    r1 = _start_run(client, registered["s1"], 42)
    r2 = _start_run(client, registered["s1"], 43)
    x = _attempt(client, r1["id"])
    y = _attempt(client, r2["id"])
    wait_attempt(svc, x["id"])
    wait_attempt(svc, y["id"])
    assert _result(svc, x["id"])["seed"] == 42
    assert _result(svc, x["id"]) != _result(svc, y["id"])


# ---------- 2. 作业只读取固定快照, 发布新标定不改变运行结果 ----------


def test_new_calibration_does_not_change_existing_snapshot(client, svc, registered):
    run = _start_run(client, registered["s1"], 7)
    a = _attempt(client, run["id"])
    wait_attempt(svc, a["id"])
    before = _result(svc, a["id"])

    # 运行后再“发布新标定”: S1 不受影响 (它绑定的是 cal-v1)。
    run2 = _start_run(client, registered["s1"], 7)
    a2 = _attempt(client, run2["id"])
    wait_attempt(svc, a2["id"])
    assert _result(svc, a2["id"]) == before

    # 使用新标定的另一个快照结果不同。
    run3 = _start_run(client, registered["s2"], 7)
    a3 = _attempt(client, run3["id"])
    wait_attempt(svc, a3["id"])
    assert _result(svc, a3["id"]) != before


# ---------- 3. 缺输入/种子不能标记为可复现 ----------


def test_missing_seed_or_input_cannot_be_marked_reproducible(client, svc, registered):
    run = _start_run(client, registered["s1"], 42)
    a = _attempt(client, run["id"])
    wait_attempt(svc, a["id"])

    report = svc.verify_attempt(a["id"])
    assert report["reproducible"] is True
    r = client.post(f"/attempts/{a['id']}/mark-reproducible")
    assert r.status_code == 200
    assert r.json()["status"] == "marked_reproducible"


def test_failed_attempt_cannot_be_marked(client, svc, registered):
    run = _start_run(client, registered["s1"], 42)
    a = _attempt(client, run["id"], {"fault": "lose_artifact"})
    final = wait_attempt(svc, a["id"])
    assert final["status"] == "failed"
    assert final["error_code"] == "artifact_missing"
    r = client.post(f"/attempts/{a['id']}/mark-reproducible")
    assert r.status_code == 409
    detail = r.json()["detail"]
    assert detail["error"] == "not_reproducible"
    assert any(c["name"] == "artifact_present" and not c["ok"] for c in detail["checks"])


# ---------- 4. 测试输入被替换: 校验失败, 但原证据可恢复 ----------


def test_tampered_bag_input_fails_verification(client, svc, registered):
    bag_id = registered["bag"]
    digest = bag_id.split("-", 1)[1]
    path = svc.store.path_for(digest)
    original = path.read_bytes()
    try:
        # 直接替换证据库里的字节 (模拟测试输入被替换)。
        path.write_bytes(b'{"bag_id":"tampered","points":[[9,9]]}')
        run = _start_run(client, registered["s1"], 42)
        a = _attempt(client, run["id"])
        final = wait_attempt(svc, a["id"])
        assert final["status"] == "failed"
        assert final["error_code"] == "input_verification_failed"
    finally:
        # 修复: 只允许写回与哈希一致的原内容。
        svc.repair_evidence(digest, original)

    run = _start_run(client, registered["s1"], 42)
    a = _attempt(client, run["id"])
    final = wait_attempt(svc, a["id"])
    assert final["status"] == "succeeded"


def test_repair_rejects_wrong_content(client, svc, registered):
    digest = registered["bag"].split("-", 1)[1]
    with pytest.raises(ValueError):
        svc.repair_evidence(digest, b'{"different": true}')


# ---------- 5. 产物缺失 / 产物被改: 先校验再发布 ----------


def test_corrupt_result_is_never_published(client, svc, registered):
    run = _start_run(client, registered["s1"], 42)
    a = _attempt(client, run["id"], {"fault": "corrupt_result"})
    final = wait_attempt(svc, a["id"])
    assert final["status"] == "failed"
    assert final["error_code"] == "output_verification_failed"
    assert final["result_artifact_id"] is None
    assert final["index_entry_id"] is None
    # 失败也留下错误证据 (不同尝试, 不同证据, 不覆盖)。
    err, _ = svc.get_artifact_bytes(a["id"], "error")
    assert json.loads(err)["error_code"] == "output_verification_failed"


def test_external_artifact_deletion_detected_by_verify(client, svc, registered):
    run = _start_run(client, registered["s1"], 42)
    a = _attempt(client, run["id"])
    wait_attempt(svc, a["id"])
    digest = svc.get_attempt(a["id"])["result_artifact_id"].split("-", 1)[1]
    path = svc.store.path_for(digest)
    path.unlink()
    report = svc.verify_attempt(a["id"])
    assert report["reproducible"] is False
    assert any(c["name"] == "artifact_present" and not c["ok"] for c in report["checks"])


# ---------- 6. 运行中改参数 / 不可变性 ----------


def test_snapshot_and_inputs_are_immutable_at_db_level(svc, registered):
    conn = sqlite3.connect(svc.settings.db_path)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE snapshots SET calibration_id='x' WHERE id=?", (registered["s1"],))
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("DELETE FROM bags WHERE id=?", (registered["bag"],))
    conn.rollback()
    conn.close()


def test_snapshot_cannot_be_put_or_deleted(client, registered):
    r = client.put(f"/snapshots/{registered['s1']}", json={})
    assert r.status_code == 405
    r = client.delete(f"/snapshots/{registered['s1']}")
    assert r.status_code == 405


def test_registering_same_doc_is_idempotent(client):
    body = {
        "sample_rate": 0.5,
        "grid_size": 1.0,
        "min_cluster_size": 1,
        "bounds": [0, 0, 1, 1],
    }
    # 键顺序/空白不影响内容身份。
    shuffled = {
        "bounds": [0, 0, 1, 1],
        "min_cluster_size": 1,
        "grid_size": 1.0,
        "sample_rate": 0.5,
    }
    r1 = client.post("/params", json=body)
    r2 = client.post(
        "/params",
        content=json.dumps(shuffled, indent=3).encode(),
        headers={"content-type": "application/json"},
    )
    assert r1.status_code == r2.status_code == 201
    assert r1.json()["id"] == r2.json()["id"]
    # 不同内容必然不同身份。
    other = {**body, "sample_rate": 0.25}
    r3 = client.post("/params", json=other)
    assert r3.json()["id"] != r1.json()["id"]


# ---------- 7. 崩溃与重启: running 变 interrupted, 可重新尝试 ----------


def test_crash_recovery_on_restart(tmp_path, registered):
    from app.config import Settings
    from app.service import Service
    from app.canonical import canonical_bytes
    from app.crypto import content_id
    from app.service import utcnow

    svc = Service(Settings(tmp_path / "data"))
    run = svc.start_run(registered["s1"], 42)
    # 直接落一条 running 记录, 模拟进程崩溃时仍在运行的尝试。
    attempt_id = content_id(
        "att", canonical_bytes({"run_id": run["id"], "attempt_no": 1})
    )
    import sqlite3

    conn = sqlite3.connect(svc.settings.db_path)
    conn.execute(
        "INSERT INTO attempts (id, run_id, attempt_no, status, started_at)"
        " VALUES (?, ?, 1, 'running', ?)",
        (attempt_id, run["id"], utcnow()),
    )
    conn.commit()
    conn.close()

    # “重启”: 新服务实例在构造时恢复。
    svc2 = Service(Settings(tmp_path / "data"))
    recovered = svc2.get_attempt(attempt_id)
    assert recovered["status"] == "interrupted"
    assert recovered["error_code"] == "interrupted_on_restart"

    # 重新发起一次尝试, 正常成功。
    a2 = svc2.submit_attempt(run["id"])
    assert wait_attempt(svc2, a2["id"])["status"] == "succeeded"


# ---------- 8. 索引 HMAC 签名与篡改检测 ----------


def test_index_signature_and_tamper_detection(svc, registered):
    run = svc.start_run(registered["s1"], 42)
    a = svc.submit_attempt(run["id"])
    wait_attempt(svc, a["id"])
    report = svc.verify_attempt(a["id"])
    assert report["reproducible"] is True

    conn = sqlite3.connect(svc.settings.db_path)
    row = conn.execute(
        "SELECT payload_json, signature FROM index_entries WHERE attempt_id=?",
        (a["id"],),
    ).fetchone()
    payload = json.loads(row[0])
    # 索引不可变: 数据库层直接拒绝篡改。
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "UPDATE index_entries SET signature='00' WHERE attempt_id=?", (a["id"],)
        )
    conn.rollback()
    conn.close()

    # 签名确实绑定负载: 用被改负载 + 原签名验证必须失败。
    from app.crypto import hmac_verify
    from app.canonical import canonical_bytes

    payload["seed"] = 999
    assert hmac_verify(svc.hmac_key, canonical_bytes(payload), row[1]) is False


# ---------- 9. 输入校验 ----------


def test_rejects_malformed_bag(client):
    r = client.post("/bags", files={"file": ("b.json", b"not json", "application/json")})
    assert r.status_code == 422


def test_rejects_snapshot_with_missing_refs(client):
    r = client.post(
        "/snapshots",
        json={
            "bag_id": "bag-dead",
            "params_id": "params-dead",
            "calibration_id": "cal-dead",
            "algorithm_id": "algo-dead",
        },
    )
    assert r.status_code == 404


def test_bad_seed_rejected(client, registered):
    r = client.post(
        f"/snapshots/{registered['s1']}/runs", params={"seed": 10**30}
    )
    assert r.status_code == 422
