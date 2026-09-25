"""KeyStore 测试：持久化、nonce 计数器先落盘后分配、文件权限。"""

from __future__ import annotations

import json
import os
import stat

import pytest

from envelope.errors import KeyNotFoundError
from envelope.keystore import KeyStore


def test_generate_and_reload(tmp_path):
    store_dir = tmp_path / "keys"
    ks = KeyStore(store_dir)
    e1 = ks.generate_master_key()
    e2 = ks.generate_master_key()

    assert e1.kid != e2.kid and e1.kid.startswith("mk_")
    assert len(e1.key_material) == 32
    assert ks.latest_kid() == e2.kid

    # 重新打开后状态完整保留
    ks2 = KeyStore(store_dir)
    assert {e.kid for e in ks2.list_keys()} == {e1.kid, e2.kid}
    assert ks2.get(e1.kid).key_material == e1.key_material


def test_unknown_key(tmp_path):
    ks = KeyStore(tmp_path / "keys")
    with pytest.raises(KeyNotFoundError):
        ks.get("mk_does_not_exist")


def test_wrap_counter_persists_and_never_repeats(tmp_path):
    ks = KeyStore(tmp_path / "keys")
    values = [ks.allocate_wrap_counter() for _ in range(5)]
    assert values == [1, 2, 3, 4, 5]

    # 模拟进程重启：计数器从磁盘恢复，绝不回退
    ks2 = KeyStore(tmp_path / "keys")
    assert ks2.allocate_wrap_counter() == 6

    raw = json.loads((tmp_path / "keys" / "keystore.json").read_text())
    assert raw["next_wrap_counter"] == 7


def test_file_permissions_are_restricted(tmp_path):
    store_dir = tmp_path / "keys"
    KeyStore(store_dir).generate_master_key()
    mode_dir = stat.S_IMODE(os.stat(store_dir).st_mode)
    mode_file = stat.S_IMODE(os.stat(store_dir / "keystore.json").st_mode)
    assert mode_dir == 0o700
    assert mode_file == 0o600
