"""Real Ed25519 signature / fingerprint tests."""

from app import crypto


def test_canonical_json_deterministic_and_signature_stripped():
    p1 = {"b": 1, "a": [1, 2, {"z": 0}], "signature": "deadbeef"}
    p2 = {"a": [1, 2, {"z": 0}], "b": 1}
    assert crypto.canonical_json(p1) == crypto.canonical_json(p2)
    assert b"signature" not in crypto.canonical_json(p1)


def test_fingerprint_sha256_known_value():
    import hashlib
    import json

    p = {"x": 1}
    canon = json.dumps(p, sort_keys=True, separators=(",", ":")).encode()
    assert crypto.fingerprint(p) == hashlib.sha256(canon).hexdigest()


def test_sign_and_verify_roundtrip():
    priv, pub = crypto.generate_keypair()
    payload = {"calibration_version": "v9", "edges": [1, 2, 3]}
    sig = crypto.sign(priv, payload)
    assert crypto.verify(pub, payload, sig) is True
    # tampered payload fails
    assert crypto.verify(pub, {**payload, "edges": [1, 2, 9]}, sig) is False
    # malformed signature fails rather than raising
    assert crypto.verify(pub, payload, "not-hex-signature") is False


def test_verify_bytes_and_hex_key():
    priv, pub = crypto.generate_keypair()
    blob = b"calibration-blob"
    sig = crypto.sign_bytes(priv, blob)
    pub2 = crypto.public_key_from_hex(crypto.public_key_hex(pub))
    assert crypto.verify_bytes(pub2, blob, sig) is True
    assert crypto.verify_bytes(pub2, blob + b"x", sig) is False


def test_wrong_key_rejects():
    priv1, _ = crypto.generate_keypair()
    _, pub2 = crypto.generate_keypair()
    payload = {"a": 1}
    sig = crypto.sign(priv1, payload)
    assert crypto.verify(pub2, payload, sig) is False


def test_envelope_carries_real_signature():
    priv, pub = crypto.generate_keypair()
    env = crypto.envelope({"result": "ok"}, priv)
    assert env["signature_algorithm"] == "Ed25519"
    assert "fingerprint_sha256" in env
    assert crypto.verify(pub, env, env["signature"]) is True
