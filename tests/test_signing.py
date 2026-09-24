"""Cryptographic trust chain: canonical signing, verification, tampering."""

import json

import pytest

from app.errors import PolicyValidationError, TrustError
from app.signing import (
    TrustStore,
    canonical_json,
    generate_keypair,
    key_id,
    load_private_key_pem,
    private_key_pem,
    public_key_pem,
    sign_document,
)


def test_roundtrip_verifies(trust, signer, demo_policy):
    bundle = signer(demo_policy)
    document = trust.verify_bundle(bundle)
    assert document["id"] == demo_policy["id"]


def test_canonical_encoding_is_key_order_independent():
    a = {"b": 1, "a": [1, 2, {"z": 0, "y": 0}]}
    b = {"a": [1, 2, {"y": 0, "z": 0}], "b": 1}
    assert canonical_json(a) == canonical_json(b)


def test_wrong_kid_rejected(keypair):
    store = TrustStore()
    _priv, pub = keypair
    bundle = sign_document({"id": "p", "rules": []}, keypair[0])
    bundle["kid"] = "deadbeef" * 4
    with pytest.raises(TrustError, match="no trusted key"):
        store.verify_bundle(bundle)


def test_unknown_signer_rejected(trust, demo_policy):
    attacker, _ = generate_keypair()
    bundle = sign_document(demo_policy, attacker)
    with pytest.raises(TrustError, match="no trusted key"):
        trust.verify_bundle(bundle)


def test_tampered_document_rejected(trust, signer, demo_policy):
    bundle = signer(demo_policy)
    tampered = json.loads(json.dumps(bundle))
    tampered["document"]["rules"][0]["effect"] = "permit"
    with pytest.raises(TrustError, match="signature verification failed"):
        trust.verify_bundle(tampered)


def test_tampered_signature_rejected(trust, signer, demo_policy):
    import base64

    bundle = signer(demo_policy)
    raw = bytearray(base64.urlsafe_b64decode(bundle["sig"] + "=="))
    raw[0] ^= 0xFF  # deterministically flip a signature byte
    bundle["sig"] = base64.urlsafe_b64encode(bytes(raw)).rstrip(b"=").decode()
    with pytest.raises(TrustError):
        trust.verify_bundle(bundle)


def test_tampered_alg_rejected(trust, signer, demo_policy):
    bundle = signer(demo_policy)
    bundle["alg"] = "HS256"
    with pytest.raises(TrustError, match="alg"):
        trust.verify_bundle(bundle)


def test_non_ed25519_key_rejected():
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    rsa_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    rsa_pem = rsa_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    with pytest.raises(PolicyValidationError, match="Ed25519"):
        load_private_key_pem(rsa_pem)

    # Trust store rejects a non-Ed25519 public anchor as well.
    rsa_pub_pem = rsa_key.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    with pytest.raises(TrustError, match="Ed25519"):
        TrustStore().add_pem(rsa_pub_pem)


def test_trust_store_from_empty_directory(tmp_path):
    store = TrustStore.from_directory(tmp_path)
    assert store.trusted_kids == []
    priv, pub = generate_keypair()
    keyfile = tmp_path / "anchor.pub.pem"
    keyfile.write_bytes(public_key_pem(pub))
    # file suffix is .pem but name ends with .pub.pem — still loads
    store2 = TrustStore.from_directory(tmp_path)
    assert key_id(pub) in store2.trusted_kids


def test_private_key_in_trust_dir_is_ignored(tmp_path, keypair):
    # A private key dropped into the trust directory must not be loaded as a
    # trust anchor: only *.pub.pem files are read.
    priv, pub = keypair
    (tmp_path / "leaked.pem").write_bytes(private_key_pem(priv))
    store = TrustStore.from_directory(tmp_path)
    assert store.trusted_kids == []


def test_garbage_pem_in_trust_dir(tmp_path):
    (tmp_path / "bad.pub.pem").write_text("not a key")
    with pytest.raises(TrustError):
        TrustStore.from_directory(tmp_path)
