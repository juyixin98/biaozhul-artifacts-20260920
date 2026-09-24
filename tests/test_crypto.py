"""密码学操作测试：真实 SHA-256 指纹与 HMAC-SHA256 签名。"""

from __future__ import annotations

import hashlib
import hmac
import json

from app.crypto import (
    canonical_json,
    new_session_key,
    sha256_hex,
    sign_response,
    verify_signature,
)


def test_new_session_key_is_random_hex():
    k1 = new_session_key()
    k2 = new_session_key()
    assert len(k1) == 64 and len(k2) == 64
    assert k1 != k2
    int(k1, 16)  # 合法 hex


def test_sha256_matches_hashlib_and_is_order_independent():
    payload = {"b": 1, "a": [1, 2, {"c": 3}]}
    expected = hashlib.sha256(
        canonical_json(payload).encode("utf-8")
    ).hexdigest()
    assert sha256_hex(payload) == expected
    reordered = {"a": [1, 2, {"c": 3}], "b": 1}
    assert sha256_hex(payload) == sha256_hex(reordered)


def test_sha256_changes_with_content():
    assert sha256_hex({"x": 1}) != sha256_hex({"x": 2})


def test_hmac_sign_and_verify_roundtrip():
    key = new_session_key()
    body = {"frame_id": 1, "ok": True, "v": [1.5, -2.0]}
    sig = sign_response(body, key)
    assert verify_signature(body, key, sig)
    # 篡改任意字段即验签失败
    tampered = dict(body, frame_id=2)
    assert not verify_signature(tampered, key, sig)
    # 错误密钥失败
    assert not verify_signature(body, new_session_key(), sig)


def test_hmac_signature_is_deterministic():
    key = new_session_key()
    body = {"a": 1}
    assert sign_response(body, key) == sign_response(body, key)
    # 与手工 HMAC 一致
    raw = json.dumps(body, sort_keys=True, separators=(",", ":"),
                     ensure_ascii=False).encode()
    manual = hmac.new(bytes.fromhex(key), raw, hashlib.sha256).hexdigest()
    assert sign_response(body, key) == manual
