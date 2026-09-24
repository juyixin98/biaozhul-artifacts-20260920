"""密码学与元数据结构层面的单元测试。"""

from __future__ import annotations

import json

import pytest

from app import canonical, metadata as md
from app.crypto import KeyPair, keyid_for_public_hex, verify_signature
from app.errors import MetadataError, SignatureError
from app.repo_tool import build_targets

from conftest import Lab


def test_canonical_json_sorted_and_deterministic() -> None:
    a = canonical.canonical({"b": 1, "a": [1, 2, {"z": 0, "y": 1}]})
    b = canonical.canonical({"a": [1, 2, {"y": 1, "z": 0}], "b": 1})
    assert a == b
    assert a == b'{"a":[1,2,{"y":1,"z":0}],"b":1}'


def test_canonical_json_rejects_float() -> None:
    with pytest.raises(MetadataError):
        canonical.canonical({"x": 1.5})


def test_sign_and_verify_roundtrip() -> None:
    kp = KeyPair.generate()
    data = canonical.canonical({"hello": "world"})
    verify_signature(kp.public_hex(), kp.sign(data), data)


def test_signature_tamper_detected() -> None:
    kp = KeyPair.generate()
    data = b"payload"
    sig = bytearray.fromhex(kp.sign(data))
    sig[-1] ^= 0x01
    with pytest.raises(SignatureError):
        verify_signature(kp.public_hex(), sig.hex(), data)


def test_keyid_matches_tuf_style() -> None:
    kp = KeyPair.generate()
    assert kp.keyid() == keyid_for_public_hex(kp.public_hex())
    assert len(kp.keyid()) == 64


def test_wrong_type_envelope_rejected(lab: Lab) -> None:
    raw = build_targets(1, {}, lab.keys_a.targets_key, lab.expires_in(10))
    envelope = json.loads(raw)
    envelope["signed"]["_type"] = "snapshot"
    bad = json.dumps(envelope).encode()
    with pytest.raises(MetadataError):
        md.parse_envelope(bad, md.SNAPSHOT)


def test_root_must_define_referenced_key(lab: Lab) -> None:
    root = lab.root_v1()
    envelope = json.loads(root)
    # 让 timestamp 角色引用一个不存在的 keyid
    envelope["signed"]["roles"]["timestamp"]["keyids"] = ["00" * 32]
    with pytest.raises(MetadataError):
        md.parse_envelope(json.dumps(envelope).encode(), md.ROOT)


def test_root_keyid_must_match_public_key(lab: Lab) -> None:
    root = lab.root_v1()
    envelope = json.loads(root)
    keyid, keyobj = next(iter(envelope["signed"]["keys"].items()))
    other = KeyPair.generate().public_hex()
    keyobj["keyval"]["public"] = other  # keyid 与新公钥推导值不一致
    with pytest.raises(MetadataError):
        md.parse_envelope(json.dumps(envelope).encode(), md.ROOT)


def test_expires_requires_timezone(lab: Lab) -> None:
    raw = build_targets(1, {}, lab.keys_a.targets_key, lab.expires_in(10))
    envelope = json.loads(raw)
    envelope["signed"]["expires"] = "2030-01-01T00:00:00"  # 无时区
    with pytest.raises(MetadataError):
        md.parse_envelope(json.dumps(envelope).encode(), md.TARGETS)


def test_targets_require_sha256_and_length(lab: Lab) -> None:
    raw = build_targets(1, {"f": b"abc"}, lab.keys_a.targets_key, lab.expires_in(10))
    envelope = json.loads(raw)
    del envelope["signed"]["targets"]["f"]["hashes"]["sha256"]
    with pytest.raises(MetadataError):
        md.parse_envelope(json.dumps(envelope).encode(), md.TARGETS)
