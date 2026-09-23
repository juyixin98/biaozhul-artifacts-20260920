"""密码学原语必须真实：Ed25519 验签、篡改检测、确定性标识。"""
from __future__ import annotations

import pytest

from app.crypto import (
    canonical_json,
    generate_private_key,
    hash_canonical,
    public_key_hex,
    sign_payload,
    verify_signature,
)
from app.identity import asset_uid, event_key, message_id, parse_amount


def test_canonical_json_deterministic_and_compact():
    a = canonical_json({"b": 1, "a": [1, 2, {"x": "你"}]})
    b = canonical_json({"a": [1, 2, {"x": "你"}], "b": 1})
    assert a == b
    assert b'":' in a  # 紧凑键值分隔
    assert b", " not in a and b": " not in a  # 无空白
    assert "你".encode() in a  # unicode 不转义


def test_crypto_real_ed25519_roundtrip():
    sk = generate_private_key()
    pk = public_key_hex(sk)
    payload = {"amount": "100", "event_type": "LOCK", "nonce": "n-1"}
    sig = sign_payload(sk, payload)
    assert verify_signature(pk, payload, sig) is True


def test_crypto_real_ed25519_tampered_payload_rejected():
    sk = generate_private_key()
    pk = public_key_hex(sk)
    payload = {"amount": "100"}
    sig = sign_payload(sk, payload)
    # 任意改动都必须验签失败
    assert verify_signature(pk, {"amount": "101"}, sig) is False
    assert verify_signature(pk, payload, "00" * 64) is False
    other_pk = public_key_hex(generate_private_key())
    assert verify_signature(other_pk, payload, sig) is False
    assert verify_signature("not-hex", payload, sig) is False


def test_asset_uid_distinguishes_same_contract_on_two_chains():
    # 相同合约地址、相同 tokenID，但源链不同 => UID 不同（防跨链碰撞）
    uid_a = asset_uid("chainA", "0xDEAD", "7")
    uid_b = asset_uid("chainB", "0xDEAD", "7")
    assert uid_a != uid_b
    # 确定性
    assert asset_uid("chainA", "0xDEAD", "7") == uid_a
    # tokenID 不同也不同
    assert asset_uid("chainA", "0xDEAD", "8") != uid_a


def test_message_and_event_ids_are_real_sha256():
    mid = message_id("chainA", "nonce-9")
    ekey = event_key("chainA", "0xabc", 3)
    assert len(mid) == 64 and mid == hash_canonical(["message-id/v1", "chainA", "nonce-9"])
    assert len(ekey) == 64
    assert event_key("chainA", "0xabc", 3) == ekey
    assert event_key("chainA", "0xabc", 4) != ekey


def test_amount_is_exact_integer():
    assert parse_amount("123456789012345678901234567890") == 123456789012345678901234567890
    with pytest.raises(ValueError):
        parse_amount("1.5")
    with pytest.raises(ValueError):
        parse_amount(-1)
    with pytest.raises(ValueError):
        parse_amount(True)
