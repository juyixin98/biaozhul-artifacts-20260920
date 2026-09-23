"""pytest 公共夹具。"""

from __future__ import annotations

import os

import pytest

from envelope_enc.keyring import Keyring


@pytest.fixture()
def keyring(tmp_path) -> Keyring:
    """每个测试一把全新的本地密钥环（含一把 active 主密钥）。"""
    kr = Keyring(str(tmp_path / "keys.json"))
    kr.initialize()
    return kr


@pytest.fixture()
def make_path(tmp_path):
    """集中产生测试文件路径。"""
    def make(name: str) -> str:
        return str(tmp_path / name)

    make.dir = tmp_path
    return make


def list_files(directory) -> list[str]:
    return sorted(os.listdir(str(directory)))
