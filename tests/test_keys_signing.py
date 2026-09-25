import json
import os
import stat
from pathlib import Path

import pytest

from mde.keys import KEYS_VERSION, KeyError_, load_or_create
from mde.signing import sign, verify


def test_generate_load_roundtrip(tmp_path: Path):
    keys, created = load_or_create(tmp_path)
    assert created is True
    path = tmp_path / "test-keys.json"
    mode = stat.S_IMODE(path.stat().st_mode)
    assert mode == 0o600

    keys2, created2 = load_or_create(tmp_path)
    assert created2 is False
    assert keys.key_id == keys2.key_id
    assert keys.transform_secret == keys2.transform_secret

    payload = json.loads(path.read_text())
    assert payload["version"] == KEYS_VERSION
    assert "LOCAL TEST KEYS" in payload["note"]


def test_sign_verify_positive_and_negative(tmp_path: Path):
    keys, _ = load_or_create(tmp_path)
    obj = {"a": 1, "b": [1, 2, 3]}
    sig = sign(keys.sign_private, obj)
    ok, _ = verify(keys.sign_public, obj, sig)
    assert ok

    other_keys = load_or_create(tmp_path / "other")[0]
    ok, reason = verify(other_keys.sign_public, obj, sig)
    assert not ok and reason == "Ed25519 验签失败"

    tampered = dict(obj, a=2)
    ok, reason = verify(keys.sign_public, tampered, sig)
    assert not ok

    # 覆盖字段不符也必须失败（防止签名被挪用到另一个对象结构）
    sig2 = dict(sig)
    sig2["covered_fields"] = ["a"]
    ok, reason = verify(keys.sign_public, obj, sig2)
    assert not ok


def test_corrupt_key_file(tmp_path: Path):
    tmp_path.mkdir(exist_ok=True)
    (tmp_path / "test-keys.json").write_text("{not json")
    with pytest.raises(KeyError_):
        load_or_create(tmp_path)
