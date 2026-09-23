"""密码学真实执行测试：Ed25519 签名/验签、防篡改、canonical、报告哈希链。"""
from __future__ import annotations

import copy

import pytest
from nacl.exceptions import BadSignatureError

from app import crypto


def test_event_sign_verify_roundtrip(feeder_kp):
    ev = {"event_id": "e1", "ts": 10, "type": "borrow",
          "payload": {"amount": "1.5"}}
    sig = feeder_kp.sign(crypto.event_signing_bytes(ev)).signature
    vk = feeder_kp.signing_key.verify_key
    vk.verify(crypto.event_signing_bytes(ev), sig)  # 不抛异常即通过


def test_tampered_payload_fails(feeder_kp):
    ev = {"event_id": "e1", "ts": 10, "type": "borrow",
          "payload": {"amount": "1.5"}}
    sig = feeder_kp.sign(crypto.event_signing_bytes(ev)).signature
    vk = feeder_kp.signing_key.verify_key

    tampered = copy.deepcopy(ev)
    tampered["payload"]["amount"] = "999"
    with pytest.raises(BadSignatureError):
        vk.verify(crypto.event_signing_bytes(tampered), sig)

    tampered2 = copy.deepcopy(ev)
    tampered2["ts"] = 11
    with pytest.raises(BadSignatureError):
        vk.verify(crypto.event_signing_bytes(tampered2), sig)


def test_canonical_is_deterministic_and_compact():
    a = {"b": 1, "a": [1, 2, {"z": 1, "y": 2}]}
    b = {"a": [1, 2, {"y": 2, "z": 1}], "b": 1}
    assert crypto.canonical(a) == crypto.canonical(b)
    assert crypto.canonical(a) == b'{"a":[1,2,{"y":2,"z":1}],"b":1}'


def test_report_digest_chain_and_server_signature(feeder_kp, server_kp):
    def make(version, prev):
        return {
            "version": version,
            "prev_hash": prev,
            "event_ids": [f"e{i}" for i in range(version)],
            "created_from_ts": 100 + version,
            "created_at": 200,
        }

    r1 = make(1, None)
    d1 = crypto.digest_hex(r1)
    r2 = make(2, d1)
    d2 = crypto.digest_hex(r2)
    assert d1 != d2
    assert r2["prev_hash"] == d1

    sig = server_kp.sign(crypto.report_digest(r2)).signature
    server_kp.signing_key.verify_key.verify(crypto.report_digest(r2), sig)

    # 篡改被签名的报告字段 -> 验签失败
    r2_evil = copy.deepcopy(r2)
    r2_evil["version"] = 999
    with pytest.raises(BadSignatureError):
        server_kp.signing_key.verify_key.verify(crypto.report_digest(r2_evil), sig)
