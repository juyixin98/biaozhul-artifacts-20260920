"""哈希链证据：完整性、篡改检测、落盘输入可复验。"""

import json

from tests.conftest import submit_dir


def test_chain_is_valid_after_submission(client, examples):
    r = submit_dir(client, examples / "01-exact")
    jid = r.json()["job_id"]
    ev = client.get(f"/api/v1/jobs/{jid}/evidence").json()
    chain = ev["chain_verification"]
    assert chain["valid"] is True
    assert chain["entries"] == len(ev["evidence"])
    assert chain["head_matches"] and chain["report_matches"]
    # 每条证据的 self_hash = sha256(prev_hash || check_json)
    import hashlib

    prev = "0" * 64
    for e in ev["evidence"]:
        expect = hashlib.sha256(prev.encode() + e["check_json"].encode()).hexdigest()
        assert e["self_hash"] == expect
        prev = e["self_hash"]
    assert prev == chain["chain_head"]


def test_tampering_a_check_is_detected(storage, client, examples):
    r = submit_dir(client, examples / "01-exact")
    jid = r.json()["job_id"]
    before = storage.verify_chain(jid)
    first_seq = 0
    row = storage.get_evidence(jid)[first_seq]
    tampered_json = row["check_json"].replace('"severity":"INFO"', '"severity":"FAIL"')
    assert tampered_json != row["check_json"], "测试前提：第一条证据应为 INFO 才能改写"
    # 直接篡改数据库中的一条证据，模拟事后改库（self_hash 不会重算）
    with storage._conn:  # noqa: SLF001
        storage._conn.execute(
            "UPDATE evidence SET check_json=? WHERE job_id=? AND seq=?",
            (tampered_json, jid, first_seq),
        )
    chain = storage.verify_chain(jid)
    assert chain["valid"] is False
    assert chain["tampered_at"] == [first_seq], "必须指出被篡改的证据序号"
    # 未篡改时链是完整的
    assert before["valid"] is True


def test_tampering_report_is_detected(storage, client, examples):
    r = submit_dir(client, examples / "01-exact")
    jid = r.json()["job_id"]
    report = json.loads(storage.get_job(jid)["report_json"])
    report["verdict"] = "EXACT"  # 强行改裁决
    with storage._conn:  # noqa: SLF001
        storage._conn.execute(
            "UPDATE jobs SET report_json=? WHERE id=?", (json.dumps(report, sort_keys=True), jid)
        )
    chain = storage.verify_chain(jid)
    assert chain["report_matches"] is False
    assert chain["valid"] is False


def test_artifacts_persisted_for_recheck(client, examples, storage):
    r = submit_dir(client, examples / "01-exact")
    body = r.json()
    jid = body["job_id"]
    job = storage.get_job(jid)
    import os

    adir = job["artifact_dir"]
    assert os.path.exists(os.path.join(adir, "config.json"))
    assert os.path.exists(os.path.join(adir, "compilerOutput.json"))
    assert os.path.exists(os.path.join(adir, "target_deployed.hex"))
    assert os.path.isdir(os.path.join(adir, "sources"))
    manifest = json.loads(open(os.path.join(adir, "manifest.json")).read())
    assert manifest["input_sha256"] == body["input_sha256"]
    assert "src/Vault.sol" in manifest["sources"]
