import copy
import json
import threading
from datetime import datetime, timezone
from pathlib import Path

import pytest

from mde.exporter import ExportService, verify_package
from mde.keys import generate_keys, save_keys
from mde.policy import PolicyStore


POLICY_V1 = {
    "version": "mde/policy@v1",
    "policy_id": "cust",
    "revision": 1,
    "purposes": {
        "analytics": {
            "default": "deny",
            "allow": ["id", "tags[]"],
            "deny": ["ssn"],
            "generalize": {"email": {"transform": "email_domain", "params": {}}},
        }
    },
    "aliases": {},
}

POLICY_V2 = {
    **POLICY_V1,
    "revision": 2,
    "purposes": {
        "analytics": {
            "default": "deny",
            "allow": ["id"],
            "deny": ["ssn", "tags"],
            "generalize": {
                "email": {
                    "transform": "pseudonymize",
                    "params": {"scope": "purpose"},
                }
            },
        }
    },
}

RECORDS = [
    {"id": "C-1", "email": "a@example.com", "ssn": "S1", "tags": ["x"]},
    {"id": "C-2", "email": "b@example.net", "ssn": "S2", "tags": ["y", "z"]},
]


@pytest.fixture()
def service(tmp_path: Path):
    keys = generate_keys()
    save_keys(keys, tmp_path / "keys")
    store = PolicyStore(tmp_path / "policies")
    now = datetime(2026, 9, 24, tzinfo=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    stored = store.publish(POLICY_V1, now)
    svc = ExportService(store, keys)
    return svc, keys, stored.policy.policy_fingerprint, now


def test_export_package_structure_and_pin(service):
    svc, keys, fp, now = service
    pkg = svc.export(records=RECORDS, purpose="analytics", policy_fingerprint=fp,
                     task_id="t1", created_at=now)
    assert pkg["task"]["policy_fingerprint"] == fp
    assert pkg["task"]["policy_revision"] == 1
    assert pkg["manifest"]["record_count"] == 2
    # 内嵌策略快照
    assert pkg["policy_snapshot"]["revision"] == 1
    # 输出符合 v1 策略：email 泛化为域名，tags 保留
    assert pkg["output"][0] == {"id": "C-1", "email": "example.com", "tags": ["x"]}
    # 明确的免责声明：不声称匿名化
    joined = " ".join(pkg["disclaimers"])
    assert "不是匿名化" in joined
    # 签名存在且可验
    rep = verify_package(pkg, public_key=keys.sign_public)
    assert rep["overall_passed"], rep


def test_end_to_end_replay_with_records(service):
    svc, keys, fp, now = service
    pkg = svc.export(records=RECORDS, purpose="analytics", policy_fingerprint=fp)
    rep = verify_package(pkg, public_key=keys.sign_public, records=RECORDS, keys=keys)
    names = {c["check"] for c in rep["checks"]}
    assert {"ed25519_signature", "records_digest", "replay_output",
            "replay_decisions", "decision_output_consistency"} <= names
    assert rep["overall_passed"], rep


def test_tamper_with_output_is_detected(service):
    svc, keys, fp, now = service
    pkg = svc.export(records=RECORDS, purpose="analytics", policy_fingerprint=fp)
    tampered = copy.deepcopy(pkg)
    tampered["output"][0]["ssn"] = "INJECTED"
    rep = verify_package(tampered, public_key=keys.sign_public)
    assert not rep["overall_passed"]
    failed = {c["check"] for c in rep["checks"] if c["status"] == "failed"}
    # 摘要、签名（被签内容含 output 摘要链路）至少其一报警
    assert "output_digest" in failed or "ed25519_signature" in failed


def test_forged_signature_detected(service):
    svc, keys, fp, now = service
    pkg = svc.export(records=RECORDS, purpose="analytics", policy_fingerprint=fp)
    attacker = copy.deepcopy(pkg)
    attacker["output"][0]["id"] = "C-EVIL"
    # 用另一把密钥重签，但验签用原公钥
    from mde.signing import sign as do_sign

    body = {k: v for k, v in attacker.items() if k != "signature"}
    other = generate_keys()
    sig = do_sign(other.sign_private, body)
    sig["key_id"] = pkg["signature"]["key_id"]
    sig["public_key_spki_hex"] = pkg["signature"]["public_key_spki_hex"]
    attacker["signature"] = sig
    rep = verify_package(attacker, public_key=keys.sign_public)
    assert not rep["overall_passed"]
    assert any(c["check"] == "ed25519_signature" and c["status"] == "failed"
               for c in rep["checks"])


def test_policy_update_race_does_not_affect_pinned_task(service):
    """已发布 v1 后导出任务固定 v1 指纹；期间发布 v2 不改变任何在途/复验结果。"""
    svc, keys, fp1, now = service
    errors = []

    pkg_holder = {}
    ready = threading.Event()

    def export_v1():
        try:
            # 等待发布者开始，制造与策略更新并发
            ready.wait(timeout=5)
            pkg_holder["pkg"] = svc.export(
                records=RECORDS, purpose="analytics", policy_fingerprint=fp1)
        except Exception as e:  # noqa: BLE001
            errors.append(e)

    t = threading.Thread(target=export_v1)
    t.start()
    ready.set()
    stored2 = svc.store.publish(POLICY_V2, now)
    t.join(timeout=5)
    assert not errors

    pkg = pkg_holder["pkg"]
    # v2 已成为 latest…
    latest = svc.store.latest("cust")
    assert latest.policy.policy_fingerprint == stored2.policy.policy_fingerprint
    assert fp1 != stored2.policy.policy_fingerprint
    # …但任务固定在 v1：输出与签名仍是 v1 语义
    assert pkg["task"]["policy_fingerprint"] == fp1
    assert pkg["output"][0]["email"] == "example.com"
    assert pkg["output"][0]["tags"] == ["x"]
    rep = verify_package(pkg, public_key=keys.sign_public, records=RECORDS, keys=keys)
    assert rep["overall_passed"], rep

    # 同样的记录用 v2 指纹导出：email 变成假名，tags 被拒绝
    pkg2 = svc.export(records=RECORDS, purpose="analytics",
                      policy_fingerprint=stored2.policy.policy_fingerprint)
    assert pkg2["output"][0]["email"].startswith("hmac:")
    assert "tags" not in pkg2["output"][0]


def test_pinned_fingerprint_revision_conflict_rejected(tmp_path: Path):
    keys = generate_keys()
    store = PolicyStore(tmp_path / "policies")
    store.publish(POLICY_V1, "2026-09-24T00:00:00Z")
    conflict = {**POLICY_V1, "description": "same revision, different content"}
    from mde.policy import PolicyError

    with pytest.raises(PolicyError, match="不可变冲突"):
        store.publish(conflict, "2026-09-24T00:00:01Z")


def test_policies_on_disk_are_immutable(service, tmp_path: Path):
    svc, keys, fp, now = service
    path = tmp_path / "policies" / "policies" / f"{fp}.json"
    raw = json.loads(path.read_text())
    raw["policy"]["purposes"]["analytics"]["allow"].append("ssn")
    path.write_text(json.dumps(raw))
    # 篡改磁盘策略 -> 指纹校验失败
    from mde.policy import PolicyError

    with pytest.raises(PolicyError):
        svc.store.get(fp)


def test_decision_records_cover_every_field_and_are_verifiable(service):
    svc, keys, fp, now = service
    pkg = svc.export(records=RECORDS, purpose="analytics", policy_fingerprint=fp)
    rep = verify_package(pkg, public_key=keys.sign_public)
    consistency = next(c for c in rep["checks"]
                       if c["check"] == "decision_output_consistency")
    assert consistency["status"] == "passed"
    # 每条输入记录 4 个叶子；ssn 必须逐条被拒绝
    ssn = [d for d in pkg["decisions"] if d["shape_path"] == "ssn"]
    assert len(ssn) == 2 and all(d["decision"] == "deny" for d in ssn)
