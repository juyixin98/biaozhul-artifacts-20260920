"""Crypto tests run without ROS."""
from __future__ import annotations

import json

import pytest

from app.crypto import (
    CheckpointSignatureError,
    canonical_json,
    sign_payload,
    verify_envelope,
)


def test_canonical_json_is_stable():
    a = canonical_json({"b": 1, "a": [1, 2, {"x": 0}]})
    b = canonical_json({"a": [1, 2, {"x": 0}], "b": 1})
    assert a == b
    assert a == b'{"a":[1,2,{"x":0}],"b":1}'


def test_sign_and_verify_roundtrip():
    key = b"0123456789abcdef0123456789abcdef"
    payload = {"x": 1, "nested": {"y": [True, None, "x"]}}
    env = sign_payload(payload, key)
    out = verify_envelope(env, key)
    assert out["x"] == 1
    assert out["sig_alg"] == "HMAC-SHA256"


def test_tampered_payload_rejected():
    key = b"0123456789abcdef0123456789abcdef"
    env = sign_payload({"position": {"next_seq": 5}}, key)
    env["checkpoint"]["position"]["next_seq"] = 6
    with pytest.raises(CheckpointSignatureError):
        verify_envelope(env, key)


def test_wrong_key_rejected():
    env = sign_payload({"v": 1}, b"0123456789abcdef0123456789abcdef")
    with pytest.raises(CheckpointSignatureError):
        verify_envelope(env, b"ffffffffffffffffffffffffffffffff")


def test_alg_tag_tamper_rejected():
    key = b"0123456789abcdef0123456789abcdef"
    env = sign_payload({"v": 1}, key)
    env["checkpoint"]["sig_alg"] = "none"
    with pytest.raises(CheckpointSignatureError):
        verify_envelope(env, key)


def test_signature_field_cannot_be_replayed():
    key = b"0123456789abcdef0123456789abcdef"
    env = sign_payload({"v": 1}, key)
    env["checkpoint"]["v"] = 2  # signature no longer matches
    with pytest.raises(CheckpointSignatureError):
        verify_envelope(env, key)
