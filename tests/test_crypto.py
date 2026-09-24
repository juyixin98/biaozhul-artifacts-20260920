"""Tests for cryptographic controls: frozen-params Ed25519 and evidence chain."""
from __future__ import annotations

import json
import secrets

import pytest

from app.evidence import EvidenceLog, EvidenceTamperError
from app.params import (
    ParamsIntegrityError,
    canonical_bytes,
    load_params,
    sign_params,
)


def test_evidence_hash_chain_and_hmac_verify(tmp_path):
    log = EvidenceLog(tmp_path / "chain.jsonl", secrets.token_bytes(32))
    for i in range(5):
        log.append("test_event", "sess1", {"i": i})

    summary = log.verify()
    assert summary["ok"] is True
    assert summary["records"] == 5

    # Tamper with a body of one record: HMAC must fail.
    lines = (tmp_path / "chain.jsonl").read_text().splitlines()
    records = [json.loads(l) for l in lines]
    records[2]["body_hash"] = "0" * 64
    (tmp_path / "chain.jsonl").write_text(
        "\n".join(json.dumps(r) for r in records) + "\n"
    )
    with pytest.raises(EvidenceTamperError):
        log.verify()


def test_evidence_chain_detects_deleted_record(tmp_path):
    log = EvidenceLog(tmp_path / "chain.jsonl", secrets.token_bytes(32))
    for i in range(4):
        log.append("e", "s", {"i": i})
    lines = (tmp_path / "chain.jsonl").read_text().splitlines()
    (tmp_path / "chain.jsonl").write_text("\n".join(lines[:1] + lines[2:]) + "\n")
    with pytest.raises(EvidenceTamperError):
        log.verify()


def test_evidence_wrong_key_fails(tmp_path):
    log = EvidenceLog(tmp_path / "chain.jsonl", b"key-a" * 8)
    log.append("e", "s", {"x": 1})
    attacker = EvidenceLog(tmp_path / "chain.jsonl", b"key-b" * 8)
    with pytest.raises(EvidenceTamperError):
        attacker.verify()


def test_tampered_params_fail_signature_verification(tmp_path):
    from app.params import DEFAULT_PUBKEY_PATH
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives.serialization import (
        Encoding,
        PublicFormat,
    )

    params = load_params()
    tampered = json.loads(json.dumps(params.raw))
    tampered["pack"]["nominal_capacity_ah"] = 11.0  # silent parameter drift

    # Attacker re-signs tampered params with THEIR key; the pinned repo
    # public key stays in place and verification must fail.
    attacker_key = Ed25519PrivateKey.generate()
    params_path = tmp_path / "p.json"
    sig_path = tmp_path / "p.sig"
    key_path = tmp_path / "pub.pem"
    params_path.write_text(json.dumps(tampered))
    sig_path.write_text(sign_params(tampered, attacker_key).hex())
    key_path.write_bytes(DEFAULT_PUBKEY_PATH.read_bytes())
    with pytest.raises(ParamsIntegrityError):
        load_params(params_path, sig_path, key_path)

    # A bit flip in the legit params file without a valid signature also fails.
    from app.params import DEFAULT_SIG_PATH

    tampered2 = json.loads(json.dumps(params.raw))
    tampered2["pack"]["nominal_capacity_ah"] = 9.99
    p2 = tmp_path / "p2.json"
    p2.write_text(json.dumps(tampered2))
    s2 = tmp_path / "p2.sig"
    s2.write_text(DEFAULT_SIG_PATH.read_text())
    with pytest.raises(ParamsIntegrityError):
        load_params(p2, s2, DEFAULT_PUBKEY_PATH)

    # Canonical serialization is stable (deterministic signing input).
    assert canonical_bytes({"b": 1, "a": 2}) == canonical_bytes({"a": 2, "b": 1})
