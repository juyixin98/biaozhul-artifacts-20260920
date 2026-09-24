"""Tests for the real cryptographic primitives."""
from __future__ import annotations

from app import crypto


def test_sha256_known_vector():
    # NIST: sha256("abc")
    assert crypto.sha256_bytes(b"abc") == (
        "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"
    )


def test_hmac_roundtrip_and_tamper():
    payload = {"a": 1, "b": [1, 2, 3], "c": {"d": "中"}}
    sig = crypto.sign_payload(payload)
    assert sig.startswith("sha256=") and len(sig) == 7 + 64
    assert crypto.verify_payload(payload, sig)
    assert not crypto.verify_payload({**payload, "a": 2}, sig)
    assert not crypto.verify_payload(payload, sig.replace("a", "b"))
    assert not crypto.verify_payload(payload, "")


def test_canonical_json_is_order_independent():
    a = crypto.canonical_json({"x": 1, "y": 2})
    b = crypto.canonical_json({"y": 2, "x": 1})
    assert a == b
