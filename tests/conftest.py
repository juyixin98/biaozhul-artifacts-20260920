"""pytest 共享夹具：一个“密钥实验室”，负责签发合法更新包。

验证器（app.updater）只持有公钥；所有私钥都在这里，用来构造
合法更新以及各种攻击包。
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Iterable

import pytest

from app import metadata as md
from app import repo_tool
from app.crypto import KeyPair
from app.repo_tool import RepoKeys, build_release, build_root
from app.updater import Bundle

NOW = datetime(2026, 9, 24, 12, 0, 0, tzinfo=timezone.utc)


class Lab:
    """封装两套仓库密钥（A 当前、B 轮换目标）与签发助手。"""

    def __init__(self) -> None:
        self.now = NOW
        self.keys_a = RepoKeys.generate()
        self.keys_b = RepoKeys.generate()

    # ----- 时间 -----

    @staticmethod
    def expires_in(days: int = 30) -> datetime:
        return NOW + timedelta(days=days)

    @staticmethod
    def expires_ago(days: int = 1) -> datetime:
        return NOW - timedelta(days=days)

    def expiries(self, timestamp_days: int = 30) -> dict[str, datetime]:
        return {
            md.ROOT: self.expires_in(3650),
            md.TARGETS: self.expires_in(90),
            md.SNAPSHOT: self.expires_in(60),
            md.TIMESTAMP: self.expires_in(timestamp_days),
        }

    # ----- root -----

    def root_v1(self, signers: Iterable[KeyPair] | None = None) -> bytes:
        return build_root(
            version=1,
            keys=self.keys_a,
            signers=list(signers) if signers is not None else self.keys_a.root_keys,
            expires=self.expires_in(3650),
        )

    def root_v2_rotation(self) -> bytes:
        """合法轮换：新 root 授权 B 套密钥，但必须由 A+B 的 root 私钥共同签名。"""
        return build_root(
            version=2,
            keys=self.keys_b,
            signers=[*self.keys_a.root_keys, *self.keys_b.root_keys],
            expires=self.expires_in(3650),
        )

    def root_v3_b(self) -> bytes:
        """B 时代的下一版 root（自签即可）。"""
        return build_root(
            version=3,
            keys=self.keys_b,
            signers=self.keys_b.root_keys,
            expires=self.expires_in(3650),
        )

    # ----- 三段元数据 -----

    def release(
        self,
        keys: RepoKeys,
        versions: dict[str, int],
        files: dict[str, bytes],
        timestamp_days: int = 30,
    ) -> dict[str, bytes]:
        return build_release(
            keys=keys,
            files=files,
            versions=versions,
            expires_at=self.expiries(timestamp_days),
        )

    def bundle(
        self,
        release: dict[str, bytes],
        files: dict[str, bytes],
        root: bytes | None = None,
        fail_before_commit: bool = False,
    ) -> Bundle:
        return Bundle(
            timestamp=release[md.TIMESTAMP],
            snapshot=release[md.SNAPSHOT],
            targets=release[md.TARGETS],
            root=root,
            files=dict(files),
            fail_before_commit=fail_before_commit,
        )


def _parse(raw: bytes) -> dict:
    return json.loads(raw)


def _envelope(raw: bytes) -> dict:
    return _parse(raw)


@pytest.fixture
def lab() -> Lab:
    return Lab()


def replace_signed(raw: bytes, mutate) -> bytes:
    """解析元数据、修改 signed 段落并重新序列化（不重新签名）。"""

    envelope = _envelope(raw)
    envelope["signed"] = mutate(envelope["signed"])
    return json.dumps(envelope, separators=(",", ":")).encode("utf-8")


def tamper_signature(raw: bytes) -> bytes:
    """破坏一条签名但保持结构合法。"""

    envelope = _envelope(raw)
    sig = bytearray(bytes.fromhex(envelope["signatures"][0]["sig"]))
    sig[0] ^= 0xFF
    envelope["signatures"][0]["sig"] = sig.hex()
    return json.dumps(envelope, separators=(",", ":")).encode("utf-8")


def drop_signatures(raw: bytes) -> bytes:
    envelope = _envelope(raw)
    envelope["signatures"] = []
    return json.dumps(envelope, separators=(",", ":")).encode("utf-8")
