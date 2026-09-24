"""Ed25519 密钥工具测试。"""
from __future__ import annotations

from app import canonical, keys


def test_sign_verify_roundtrip():
    priv, pub = keys.generate_keypair()
    msg = b"artifact-bytes"
    sig = keys.sign(priv, msg)
    assert keys.verify(pub, sig, msg) is True
    assert len(sig) == 64


def test_verify_rejects_tampered_message():
    priv, pub = keys.generate_keypair()
    sig = keys.sign(priv, b"original")
    assert keys.verify(pub, sig, b"tampered") is False


def test_verify_rejects_foreign_signature():
    priv1, _ = keys.generate_keypair()
    _, pub2 = keys.generate_keypair()
    sig = keys.sign(priv1, b"msg")
    assert keys.verify(pub2, sig, b"msg") is False


def test_pem_roundtrip_and_key_id_stable():
    priv, pub = keys.generate_keypair()
    pub2 = keys.load_public_pem(keys.public_pem(pub))
    priv2 = keys.load_private_pem(keys.private_pem(priv))
    assert keys.public_raw(pub) == keys.public_raw(pub2)
    assert keys.verify(pub2, keys.sign(priv2, b"x"), b"x")
    assert keys.key_id(pub) == keys.key_id(pub2)
    assert len(keys.key_id(pub)) == 64  # sha256 hex


def test_raw_key_roundtrip():
    _, pub = keys.generate_keypair()
    raw = keys.public_raw(pub)
    assert len(raw) == 32
    assert keys.public_raw(keys.public_from_raw(raw)) == raw


def test_load_rejects_non_ed25519():
    import pytest
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    rsa_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    rsa_pem = rsa_key.public_key().public_bytes(
        serialization.Encoding.PEM,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    with pytest.raises(ValueError):
        keys.load_public_pem(rsa_pem)


def test_canonical_domains_distinct():
    d = bytes.fromhex("aa" * 32)
    art = canonical.artifact_message(d, "t", "1", b"n")
    root = canonical.root_message(1, 1, [d], 1, [d])
    appr = canonical.root_approval_message(root, b"n")
    assert art[:20] != root[:20] != appr[:20]
    assert art.startswith(b"ARTIFACT-SIGNATURE")
    assert root.startswith(b"TRUST-ROOT-DESCRIPTOR")
    assert appr.startswith(b"TRUST-ROOT-ROTATION-APPROVAL")


def test_canonical_field_changes_change_message():
    d = bytes.fromhex("aa" * 32)
    base = canonical.artifact_message(d, "report", "1.0.0", b"nonce")
    assert canonical.artifact_message(bytes.fromhex("bb" * 32), "report", "1.0.0", b"nonce") != base
    assert canonical.artifact_message(d, "image", "1.0.0", b"nonce") != base
    assert canonical.artifact_message(d, "report", "1.0.1", b"nonce") != base
    assert canonical.artifact_message(d, "report", "1.0.0", b"other") != base


def test_root_message_order_independent():
    k1 = bytes(range(32))
    k2 = bytes(range(1, 33))
    m1 = canonical.root_message(2, 2, [k1, k2], 1, [k2])
    m2 = canonical.root_message(2, 2, [k2, k1], 1, [k2])
    assert m1 == m2
