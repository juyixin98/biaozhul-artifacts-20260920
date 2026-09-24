"""漏洞馈送：Ed25519 签名校验、篡改检测、未签名拒绝。"""
from __future__ import annotations

import base64
import json

import pytest
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from sbom_risk.feed import FeedError, FeedPayload, canonical_payload_bytes, load_feed


def test_fixture_feed_verifies(loaded_feed):
    assert loaded_feed.verified is True
    assert loaded_feed.kid == "test-fixture-key-2026"
    assert len(loaded_feed.payload.vulnerabilities) == 8


def test_tampered_payload_rejected(tmp_path, repo_root):
    envelope = json.loads((repo_root / "fixtures" / "vuln-feed.json").read_text())
    envelope["payload"]["vulnerabilities"][0]["ranges"] = ["*"]  # 篡改
    tampered = tmp_path / "bad.json"
    tampered.write_text(json.dumps(envelope))
    with pytest.raises(FeedError, match="签名校验失败"):
        load_feed(tampered, repo_root / "fixtures" / "keys" / "test_feed_public.pem")


def test_wrong_key_rejected(tmp_path, repo_root):
    other_priv = Ed25519PrivateKey.generate()
    other_pub = tmp_path / "other.pub.pem"
    from cryptography.hazmat.primitives import serialization
    other_pub.write_bytes(
        other_priv.public_key().public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
    )
    with pytest.raises(FeedError, match="签名校验失败"):
        load_feed(
            repo_root / "fixtures" / "vuln-feed.json",
            other_pub,
        )


def test_corrupt_base64_rejected(tmp_path, repo_root):
    envelope = json.loads((repo_root / "fixtures" / "vuln-feed.json").read_text())
    envelope["signature_b64"] = "!!!not-base64!!!"
    path = tmp_path / "badb64.json"
    path.write_text(json.dumps(envelope))
    with pytest.raises(FeedError, match="base64"):
        load_feed(path, repo_root / "fixtures" / "keys" / "test_feed_public.pem")


def test_unsigned_feed_rejected_without_key(tmp_path):
    unsigned = tmp_path / "unsigned.json"
    unsigned.write_text(json.dumps({"payload": {
        "feed_version": "x", "vulnerabilities": []
    }}))
    with pytest.raises(FeedError, match="未配置公钥"):
        load_feed(unsigned, None, allow_unsigned=False)
    loaded = load_feed(unsigned, None, allow_unsigned=True)
    assert loaded.verified is False


def test_roundtrip_sign_verify(tmp_path):
    payload_obj = FeedPayload.model_validate({"feed_version": "t", "vulnerabilities": [
        {"id": "V1", "ecosystem": "npm", "name": "x", "ranges": ["=1.0.0"]}
    ]})
    payload = payload_obj.model_dump()
    priv = Ed25519PrivateKey.generate()
    from cryptography.hazmat.primitives import serialization
    pub_path = tmp_path / "pub.pem"
    pub_path.write_bytes(
        priv.public_key().public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
    )
    sig = priv.sign(canonical_payload_bytes(payload_obj))
    envelope_path = tmp_path / "feed.json"
    envelope_path.write_text(json.dumps({
        "alg": "ed25519",
        "kid": "unit",
        "signature_b64": base64.b64encode(sig).decode(),
        "payload": payload,
    }))
    feed = load_feed(envelope_path, pub_path)
    assert feed.verified is True
    assert feed.payload.vulnerabilities[0].id == "V1"


def test_invalid_range_in_feed_rejected(tmp_path, repo_root):
    envelope = json.loads((repo_root / "fixtures" / "vuln-feed.json").read_text())
    envelope["payload"]["vulnerabilities"][0]["ranges"] = ["1.2.x || 3"]
    # 需要用有效签名重签才能走到区间校验——直接改 payload 会先被签名挡住，
    # 所以这里构造自签名信封。
    priv = Ed25519PrivateKey.generate()
    from cryptography.hazmat.primitives import serialization
    pub_path = tmp_path / "pub.pem"
    pub_path.write_bytes(
        priv.public_key().public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
    )
    payload_obj = FeedPayload.model_validate(envelope["payload"])
    payload_obj.vulnerabilities[0].ranges = ["1.2.x || 3"]
    sig = priv.sign(canonical_payload_bytes(payload_obj))
    envelope["payload"] = payload_obj.model_dump()
    envelope["signature_b64"] = base64.b64encode(sig).decode()
    path = tmp_path / "badrange.json"
    path.write_text(json.dumps(envelope))
    with pytest.raises(FeedError, match="区间非法"):
        load_feed(path, pub_path)
