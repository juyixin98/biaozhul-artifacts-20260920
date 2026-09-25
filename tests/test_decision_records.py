"""验收场景六：可核验的字段决策记录。

核验三层：
1. 清单 Ed25519 签名；
2. output / decisions 的 SHA-256；
3. 提供原始数据时，用包内策略快照全量重跑并逐字节比对。

另测：Fernet 加密封包的解密核验、错误密钥失败、篡改任一字段即失败。
"""

import copy
import json

import pytest

from mde import crypto
from mde.errors import VerificationError
from mde.service import ExportService


POLICY_RULES = [
    {"path": "name", "action": "allow"},
    {"path": "email", "action": "generalize", "generalizer": "email_mask"},
    {"path": "ssn", "action": "deny"},
    {"path": "tags[].label", "action": "allow"},
    {"path": "age", "action": "generalize", "generalizer": "numeric_bucket",
     "params": {"bins": [0, 18, 65]}},
]

DATA = {
    "name": "Alice",
    "email": "alice@example.com",
    "ssn": "123-45-6789",
    "age": 30,
    "tags": [{"label": "x", "secret": 1}],
    "injected": "no",
}


def _setup(store):
    store.publish("hr", POLICY_RULES)


def test_decision_records_cover_every_field(service, store):
    _setup(store)
    b = service.export(DATA, "hr", "analytics")
    paths = {d["path"] for d in b["decisions"]}
    # 每个出现的字段（含数组内、被拒的、默认拒绝的）都有记录。
    expected = {"name", "email", "ssn", "age", "tags", "tags[].label",
                "tags[].secret", "injected"}
    assert expected <= paths
    by_path = {d["path"]: d["decision"] for d in b["decisions"]}
    assert by_path["name"] == "allow"
    assert by_path["email"] == "generalize"
    assert by_path["ssn"] == "deny"
    assert by_path["tags[].secret"] == "deny"
    assert by_path["injected"] == "deny"
    # 决策记录里不出现被拒绝字段的原值。
    serialized = json.dumps(b["decisions"], ensure_ascii=False)
    assert "123-45-6789" not in serialized


def test_full_verification_with_source_data(service, store):
    _setup(store)
    b = service.export(DATA, "hr", "analytics")
    report = service.verify_bundle(b, source_data=DATA)
    assert report["ok"]
    names = [c["check"] for c in report["checks"]]
    assert names == [
        "manifest_signature", "policy_snapshot", "output_hash",
        "decisions_hash", "recomputed_output", "recomputed_decisions",
        "anonymization_disclaimer_present",
    ]
    assert all(c["ok"] for c in report["checks"])


def test_tamper_output_detected(service, store):
    _setup(store)
    b = service.export(DATA, "hr", "analytics")
    b["output"]["name"] = "Mallory"
    with pytest.raises(VerificationError, match="output_hash"):
        service.verify_bundle(b)


def test_tamper_decision_detected(service, store):
    _setup(store)
    b = service.export(DATA, "hr", "analytics")
    # 找到一条 deny 记录，篡改成 allow：决策记录哈希立即不符。
    deny = next(d for d in b["decisions"] if d["decision"] == "deny")
    deny["decision"] = "allow"
    with pytest.raises(VerificationError, match="decisions_hash"):
        service.verify_bundle(b)


def test_signature_forgery_detected(service, store):
    _setup(store)
    b = service.export(DATA, "hr", "analytics")
    # 用另一把私钥重签篡改过的清单，公钥指纹对不上 -> 签名失败。
    other = crypto.generate_signing_key()
    forged = copy.deepcopy(b["manifest"])
    forged["purpose"] = "fraud"
    target = ExportService._signing_target(forged)
    b["manifest"]["purpose"] = "fraud"
    b["manifest"]["signature"] = other.sign(
        __import__("mde.canonical", fromlist=["canonical_dumps"])
        .canonical_dumps(target)).hex()
    with pytest.raises(VerificationError, match="signature"):
        service.verify_bundle(b)


def test_policy_snapshot_tampering_detected(service, store):
    _setup(store)
    b = service.export(DATA, "hr", "analytics")
    # 试图把快照里的 deny 改成 allow；指纹立即不符。
    for r in b["policy_snapshot"]["rules"]:
        if r["path"] == "ssn":
            r["action"] = "allow"
    with pytest.raises(VerificationError, match="policy_snapshot"):
        service.verify_bundle(b)


def test_recomputation_detects_engine_semantics_gap(service, store):
    _setup(store)
    b = service.export(DATA, "hr", "analytics")
    # 用错误的“原始数据”（含额外字段）重算 -> 输出与决策记录均不一致。
    wrong_source = dict(DATA, sneaky="added")
    with pytest.raises(VerificationError,
                       match="recomputed_(output|decisions)"):
        service.verify_bundle(b, source_data=wrong_source)


def test_encrypted_bundle_roundtrip(service, store):
    _setup(store)
    key = crypto.generate_fernet_key()
    b = service.export(DATA, "hr", "analytics", encryption_key=key)
    assert b["schema_version"] == "mde-encrypted-bundle/v1"
    # 密文外层不含任何记录明文。
    outer = json.dumps(b, ensure_ascii=False)
    assert "Alice" not in outer and "alice@example.com" not in outer
    report = service.verify_bundle(b, encryption_key=key, source_data=DATA)
    assert report["ok"]


def test_encrypted_bundle_wrong_key(service, store):
    _setup(store)
    b = service.export(DATA, "hr", "analytics",
                       encryption_key=crypto.generate_fernet_key())
    with pytest.raises(VerificationError, match="decryption failed"):
        service.verify_bundle(
            b, encryption_key=crypto.generate_fernet_key())


def test_encrypted_bundle_requires_key(service, store):
    _setup(store)
    b = service.export(DATA, "hr", "analytics",
                       encryption_key=crypto.generate_fernet_key())
    with pytest.raises(VerificationError, match="encryption_key is required"):
        service.verify_bundle(b)


def test_encrypted_bundle_ciphertext_tamper(service, store):
    _setup(store)
    key = crypto.generate_fernet_key()
    b = service.export(DATA, "hr", "analytics", encryption_key=key)
    token = bytearray(b["ciphertext"].encode())
    token[-1] ^= 0x01
    b["ciphertext"] = token.decode("ascii", errors="ignore")
    with pytest.raises(VerificationError):
        service.verify_bundle(b, encryption_key=key)


def test_bundle_carries_no_anonymization_claim(service, store):
    _setup(store)
    b = service.export(DATA, "hr", "analytics")
    text = json.dumps(b, ensure_ascii=False).lower()
    # 明确的非匿名化声明（同时被签名覆盖）。
    assert "not anonymized" in text
    assert "no anonymization guarantee" in text
