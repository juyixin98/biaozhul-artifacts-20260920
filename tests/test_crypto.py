"""Unit tests for real cryptographic primitives."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.crypto import (
    Signer,
    b64d,
    b64e,
    canonical_json,
    digest_json,
    sha256_bytes,
    verify_json,
)


def test_canonical_json_is_order_independent():
    a = canonical_json({"b": 1, "a": [1, 2, {"x": 3}]})
    b = canonical_json({"a": [1, 2, {"x": 3}], "b": 1})
    assert a == b
    assert digest_json({"z": 9, "a": 1}) == digest_json({"a": 1, "z": 9})


def test_b64_roundtrip():
    raw = bytes(range(256))
    assert b64d(b64e(raw)) == raw
    assert b64e(b"") == ""


def test_sha256_known_vector():
    assert sha256_bytes(b"") == (
        "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
    )
    assert sha256_bytes(b"abc") == (
        "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"
    )


def test_ed25519_sign_and_verify(tmp_path: Path):
    signer = Signer(tmp_path / "k.pem")
    signer.load_or_create()
    msg = {"snapshot": "abc", "n": 3}
    sig = signer.sign_json(msg)
    assert signer.verify_json_signature(msg, sig) is True
    # Any change to the message invalidates the signature.
    assert signer.verify_json_signature({**msg, "n": 4}, sig) is False


def test_ed25519_wrong_key_rejects(tmp_path: Path):
    s1 = Signer(tmp_path / "k1.pem")
    s1.load_or_create()
    s2 = Signer(tmp_path / "k2.pem")
    s2.load_or_create()
    sig = s1.sign_json({"a": 1})
    assert verify_json({"a": 1}, sig, s2.public_pem()) is False


def test_key_persists_across_restarts(tmp_path: Path):
    path = tmp_path / "k.pem"
    s1 = Signer(path)
    s1.load_or_create()
    pem1 = s1.public_pem()
    sig = s1.sign_json({"v": 1})
    # Simulate restart: a new Signer loads the same key file.
    s2 = Signer(path)
    s2.load_or_create()
    assert s2.public_pem() == pem1
    assert s2.verify_json_signature({"v": 1}, sig) is True
    assert oct((tmp_path / "k.pem").stat().st_mode & 0o777) == "0o600"
