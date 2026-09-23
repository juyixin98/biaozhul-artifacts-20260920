"""本地密钥环测试：初始化、轮换状态、文件权限、损坏检测。"""

from __future__ import annotations

import base64
import json
import os
import stat

import pytest

from envelope_enc.crypto import KEY_LEN
from envelope_enc.keyring import Keyring, KeyringError


def test_initialize_creates_0600_file(tmp_path):
    path = str(tmp_path / "keys.json")
    kr = Keyring(path)
    mk = kr.initialize()
    assert os.path.exists(path)
    mode = stat.S_IMODE(os.stat(path).st_mode)
    assert mode == 0o600
    assert mk.status == "active"
    assert len(kr.active().key) == KEY_LEN


def test_initialize_exist_ok(tmp_path):
    path = str(tmp_path / "keys.json")
    kr = Keyring(path)
    first = kr.initialize()
    again = kr.initialize(exist_ok=True)
    assert again.kid == first.kid
    with pytest.raises(KeyringError):
        kr.initialize()


def test_rotate_master_marks_old_retired(tmp_path):
    kr = Keyring(str(tmp_path / "k.json"))
    first = kr.initialize()
    second = kr.rotate_master()
    third = kr.rotate_master()
    assert kr.active().kid == third.kid
    statuses = {mk.kid: mk.status for mk in kr.list_keys()}
    assert statuses[first.kid] == "retired"
    assert statuses[second.kid] == "retired"
    assert statuses[third.kid] == "active"
    # retired 密钥仍可取用于解密
    assert kr.get_for_unwrap(first.kid).kid == first.kid


def test_kids_are_unique(tmp_path):
    kr = Keyring(str(tmp_path / "k.json"))
    kr.initialize()
    kids = {kr.rotate_master().kid for _ in range(5)}
    assert len(kids) == 5


def test_cannot_delete_active_key(tmp_path):
    kr = Keyring(str(tmp_path / "k.json"))
    mk = kr.initialize()
    with pytest.raises(KeyringError, match="active"):
        kr.delete_key(mk.kid)


def test_delete_retired_key(tmp_path):
    kr = Keyring(str(tmp_path / "k.json"))
    old = kr.initialize()
    kr.rotate_master()
    kr.delete_key(old.kid)
    with pytest.raises(KeyringError):
        kr.get_for_unwrap(old.kid)


def test_missing_and_corrupt_keyring(tmp_path):
    with pytest.raises(KeyringError, match="不存在"):
        Keyring(str(tmp_path / "nope.json")).active()
    path = tmp_path / "bad.json"
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(KeyringError, match="损坏"):
        Keyring(str(path)).active()
    path.write_text(json.dumps({"keys": {}, "active_kid": "x"}), encoding="utf-8")
    with pytest.raises(KeyringError):
        Keyring(str(path)).active()


def test_bad_key_length_rejected(tmp_path):
    path = tmp_path / "badlen.json"
    path.write_text(
        json.dumps(
            {
                "keys": {
                    "mk-x": {
                        "key_b64": base64.b64encode(b"short").decode(),
                        "status": "active",
                    }
                },
                "active_kid": "mk-x",
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(KeyringError, match="长度"):
        Keyring(str(path)).active()
