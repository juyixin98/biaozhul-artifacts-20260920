"""仓库/签发端工具：生成密钥、构建并签名四种元数据。

验证服务本身**不包含任何私钥**，本模块仅供仓库方（以及自动化测试）
构造合法/恶意更新包使用。

一个完整版本的构建顺序：

1. :func:`build_root`（仅引导或轮换时）
2. :func:`build_targets`
3. :func:`build_snapshot` —— 固定 targets 字节
4. :func:`build_timestamp` —— 固定 snapshot 字节
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timedelta, timezone
from typing import Iterable, Sequence

from . import metadata as md
from .crypto import KeyPair


# ---------------------------------------------------------------------------
# 密钥集合
# ---------------------------------------------------------------------------


class RepoKeys:
    """一个仓库版本点上四角色各自的签名私钥（root 支持多把，便于阈值/轮换）。"""

    def __init__(
        self,
        root_keys: Sequence[KeyPair],
        targets_key: KeyPair,
        snapshot_key: KeyPair,
        timestamp_key: KeyPair,
    ) -> None:
        self.root_keys = list(root_keys)
        self.targets_key = targets_key
        self.snapshot_key = snapshot_key
        self.timestamp_key = timestamp_key

    @classmethod
    def generate(cls, root_threshold: int = 1) -> "RepoKeys":
        return cls(
            root_keys=[KeyPair.generate() for _ in range(root_threshold)],
            targets_key=KeyPair.generate(),
            snapshot_key=KeyPair.generate(),
            timestamp_key=KeyPair.generate(),
        )

    def all_keys(self) -> list[KeyPair]:
        return [
            *self.root_keys,
            self.targets_key,
            self.snapshot_key,
            self.timestamp_key,
        ]


def _dedupe(signers: Iterable[KeyPair]) -> list[KeyPair]:
    seen: set[str] = set()
    out: list[KeyPair] = []
    for signer in signers:
        kid = signer.keyid()
        if kid not in seen:
            seen.add(kid)
            out.append(signer)
    return out


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def file_meta_dict(data: bytes) -> dict:
    return {"length": len(data), "hashes": {"sha256": sha256_hex(data)}}


def _utc_expires(expires: datetime | str) -> datetime | str:
    if isinstance(expires, str):
        return expires
    if expires.tzinfo is None:
        return expires.replace(tzinfo=timezone.utc)
    return expires


# ---------------------------------------------------------------------------
# 四段元数据的构建
# ---------------------------------------------------------------------------


def build_root(
    version: int,
    keys: RepoKeys,
    signers: Iterable[KeyPair],
    expires: datetime | str,
    root_threshold: int = 1,
) -> bytes:
    """构建 root 元数据。

    ``keys`` 决定本 root 授权哪些公钥；``signers`` 决定由哪些私钥实际签名
    （轮换时应同时传入旧、新两套 root 私钥）。
    """

    all_keys = keys.all_keys()
    body = md.make_signed(
        md.ROOT,
        version,
        _utc_expires(expires),
        {
            "keys": md.root_keys_section(all_keys),
            "roles": {
                md.ROOT: md.root_role_spec(keys.root_keys, root_threshold),
                md.TARGETS: md.root_role_spec([keys.targets_key], 1),
                md.SNAPSHOT: md.root_role_spec([keys.snapshot_key], 1),
                md.TIMESTAMP: md.root_role_spec([keys.timestamp_key], 1),
            },
        },
    )
    return md.build_envelope(body, _dedupe(signers))


def build_targets(
    version: int,
    files: dict[str, bytes],
    signer: KeyPair,
    expires: datetime | str,
) -> bytes:
    targets = {name: file_meta_dict(data) for name, data in sorted(files.items())}
    body = md.make_signed(
        md.TARGETS, version, _utc_expires(expires), {"targets": targets}
    )
    return md.build_envelope(body, [signer])


def build_snapshot(
    version: int,
    targets_raw: bytes,
    targets_version: int,
    signer: KeyPair,
    expires: datetime | str,
) -> bytes:
    body = md.make_signed(
        md.SNAPSHOT,
        version,
        _utc_expires(expires),
        {
            "meta": {
                "targets.json": {
                    "version": targets_version,
                    **file_meta_dict(targets_raw),
                }
            }
        },
    )
    return md.build_envelope(body, [signer])


def build_timestamp(
    version: int,
    snapshot_raw: bytes,
    snapshot_version: int,
    signer: KeyPair,
    expires: datetime | str,
) -> bytes:
    body = md.make_signed(
        md.TIMESTAMP,
        version,
        _utc_expires(expires),
        {
            "meta": {
                "snapshot.json": {
                    "version": snapshot_version,
                    **file_meta_dict(snapshot_raw),
                }
            }
        },
    )
    return md.build_envelope(body, [signer])


# ---------------------------------------------------------------------------
# 一个版本包的便捷组装
# ---------------------------------------------------------------------------


def build_release(
    keys: RepoKeys,
    versions: dict[str, int],
    files: dict[str, bytes],
    expires_at: dict[str, datetime],
) -> dict[str, bytes]:
    """构建除 root 外的三段元数据，返回 {角色名: 字节}。"""

    targets_raw = build_targets(
        versions[md.TARGETS], files, keys.targets_key,
        expires_at[md.TARGETS],
    )
    snapshot_raw = build_snapshot(
        versions[md.SNAPSHOT],
        targets_raw,
        versions[md.TARGETS],
        keys.snapshot_key,
        expires_at[md.SNAPSHOT],
    )
    timestamp_raw = build_timestamp(
        versions[md.TIMESTAMP],
        snapshot_raw,
        versions[md.SNAPSHOT],
        keys.timestamp_key,
        expires_at[md.TIMESTAMP],
    )
    return {
        md.TARGETS: targets_raw,
        md.SNAPSHOT: snapshot_raw,
        md.TIMESTAMP: timestamp_raw,
    }


def default_expiries(
    now: datetime | None = None,
    root_days: int = 3650,
    targets_days: int = 90,
    snapshot_days: int = 60,
    timestamp_days: int = 30,
) -> dict[str, datetime]:
    """演示用默认过期窗口（timestamp 最短，root 最长）。"""

    now = now or datetime.now(timezone.utc)
    return {
        md.ROOT: now + timedelta(days=root_days),
        md.TARGETS: now + timedelta(days=targets_days),
        md.SNAPSHOT: now + timedelta(days=snapshot_days),
        md.TIMESTAMP: now + timedelta(days=timestamp_days),
    }
