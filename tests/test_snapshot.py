"""Real cryptographic checks for snapshot integrity records."""
import json

import pytest

from qos_diag.qos_model import RULES_VERSION
from qos_diag.snapshot import SnapshotStore, canonical_digest, rules_file_hash


def test_roundtrip_and_verify(tmp_path):
    store = SnapshotStore(tmp_path)
    rec = store.save({"a": 1, "b": [1, 2, 3]})
    assert rec["rules_version"] == RULES_VERSION
    assert rec["rules_hash"] == rules_file_hash()
    checks = store.verify(store.load(rec["snapshot_id"]))
    assert checks["all_ok"] is True
    # key file created with restrictive perms
    assert (tmp_path / ".key").stat().st_mode & 0o777 == 0o600


def test_body_tamper_is_detected_by_hash_and_hmac(tmp_path):
    store = SnapshotStore(tmp_path)
    rec = store.save({"a": 1})
    p = tmp_path / f"{rec['snapshot_id']}.json"
    rec["body"]["a"] = 999
    p.write_text(json.dumps(rec))
    checks = store.verify(json.loads(p.read_text()))
    assert checks["content_sha256"]["ok"] is False
    assert checks["hmac_sha256"]["ok"] is False
    # rules drift detection is independent
    assert checks["rules_hash"]["ok"] is True


def test_forged_hmac_without_key_fails(tmp_path):
    store = SnapshotStore(tmp_path)
    rec = store.save({"a": 1})
    p = tmp_path / f"{rec['snapshot_id']}.json"
    rec["body"]["a"] = 2
    rec["content_sha256"] = canonical_digest(rec["body"])
    rec["hmac_sha256"] = "0" * 64  # attacker doesn't have the key
    p.write_text(json.dumps(rec))
    checks = store.verify(json.loads(p.read_text()))
    assert checks["hmac_sha256"]["ok"] is False
    assert checks["content_sha256"]["ok"] is True


def test_explicit_key_from_env(tmp_path, monkeypatch):
    monkeypatch.setenv("QOSDIAG_SNAPSHOT_KEY", "unit-test-secret-key")
    store = SnapshotStore(tmp_path)
    rec = store.save({"x": "y"})
    checks = store.verify(rec)
    assert checks["hmac_sha256"]["ok"] is True
    # a different key must fail verification
    monkeypatch.setenv("QOSDIAG_SNAPSHOT_KEY", "different-key")
    store2 = SnapshotStore(tmp_path / "other")
    # verify with store2's key against rec signed by store1
    assert store2.verify(rec)["hmac_sha256"]["ok"] is False


def test_canonical_form_is_stable(tmp_path):
    assert canonical_digest({"a": 1, "b": 2}) == canonical_digest({"b": 2, "a": 1})
