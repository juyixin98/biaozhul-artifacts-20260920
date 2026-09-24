"""Tests for real Ed25519 signing, verification and tamper detection."""

from __future__ import annotations

import json

from app.crypto import Signer, canonical_json, verify_envelope


def test_sign_verify_roundtrip(tmp_path):
    s = Signer(tmp_path)
    env = s.sign_payload({"version_id": "v1", "alpha": 1.5})
    ok, msg = verify_envelope(env)
    assert ok, msg


def test_tampered_payload_fails_verification(tmp_path):
    s = Signer(tmp_path)
    env = s.sign_payload({"version_id": "v1", "beta": 0.001})
    env["payload"]["beta"] = 0.002  # tamper after signing
    ok, msg = verify_envelope(env)
    assert not ok
    assert "signature" in msg


def test_tampered_signature_fails(tmp_path):
    s = Signer(tmp_path)
    env = s.sign_payload({"x": 1})
    env["signature"] = env["signature"][:-2] + "AA"
    ok, _ = verify_envelope(env)
    assert not ok


def test_wrong_key_fails(tmp_path):
    a = Signer(tmp_path / "a")
    b = Signer(tmp_path / "b")
    env = a.sign_payload({"x": 1})
    body = canonical_json(env["payload"])
    forged = {
        "alg": "Ed25519",
        "public_key": b.public_key_b64(),
        "payload": env["payload"],
        "signature": env["signature"],
    }
    ok, _ = verify_envelope(forged)
    assert not ok


def test_key_persisted_on_disk_with_strict_perms(tmp_path):
    s1 = Signer(tmp_path)
    _ = s1.public_key_b64()
    pem = tmp_path / "ed25519_private.pem"
    assert pem.exists()
    mode = pem.stat().st_mode & 0o777
    assert mode == 0o600
    s2 = Signer(tmp_path)  # reloads same key
    assert s2.public_key_b64() == s1.public_key_b64()


def test_canonical_json_deterministic():
    a = canonical_json({"b": 1, "a": [1, 2, {"z": 0}]})
    b = canonical_json({"a": [1, 2, {"z": 0}], "b": 1})
    assert a == b
    assert json.loads(a) == {"a": [1, 2, {"z": 0}], "b": 1}
