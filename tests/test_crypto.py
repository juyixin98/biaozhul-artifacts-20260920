import pytest

from app.crypto import (
    canonical_json,
    generate_private_key,
    public_hex,
    public_key_from_hex,
    sign_payload,
    verify_payload,
)


def test_sign_verify_roundtrip(operator_key):
    payload = {"b": 2, "a": [1, 2, 3]}
    sig = sign_payload(operator_key, payload)
    pub = public_key_from_hex(public_hex(operator_key))
    assert verify_payload(pub, payload, sig) is True


def test_tampered_payload_fails(operator_key):
    sig = sign_payload(operator_key, {"x": 1})
    pub = operator_key.public_key()
    assert verify_payload(pub, {"x": 2}, sig) is False
    # key 顺序不同但结构相同 -> 仍然有效 (规范化序列化)
    assert verify_payload(pub, {"x": 1}, sig) is True


def test_other_key_fails(operator_key, other_key):
    sig = sign_payload(other_key, {"x": 1})
    assert verify_payload(operator_key.public_key(), {"x": 1}, sig) is False


def test_bad_signature_hex(operator_key):
    assert verify_payload(operator_key.public_key(), {"x": 1}, "zz") is False
    assert verify_payload(operator_key.public_key(), {"x": 1}, "ab") is False


def test_canonical_json_is_deterministic():
    a = canonical_json({"z": 1, "a": {"y": 2, "b": [3, 1]}})
    b = canonical_json({"a": {"b": [3, 1], "y": 2}, "z": 1})
    assert a == b
