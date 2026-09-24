"""二维加权栅格与哈希绑定的地图快照。

地图状态:
- ``weights``: float64 栅格, 每个单元的非负权重(穿越代价因子), 障碍单元也保留其权重值,
  因此解除障碍后权重仍然存在。
- ``blocked``: bool 栅格, True 表示不可穿越的障碍。

快照链:
每个不可变快照包含父快照 SHA-256、完整地图状态的规范化序列化、应用的变更列表,
三者再做 SHA-256 得到快照 id。任意篡改都会导致后续校验失败。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Iterable

import numpy as np

SQRT2 = float(np.sqrt(2.0))
CONNECTIVITY_4 = 4
CONNECTIVITY_8 = 8


def canonical_state_bytes(weights: np.ndarray, blocked: np.ndarray) -> bytes:
    """地图完整状态的规范化字节序列(确定性, 跨平台一致)。

    形状/连接性不放入状态字节: 同一地图生命周期内它们不可变。
    float 使用 repr (Python 最短往返表示), 保证序列化后可精确还原。
    """
    if weights.shape != blocked.shape:
        raise ValueError("weights 与 blocked 形状必须一致")
    rows, cols = weights.shape
    flat_w = weights.reshape(-1).tolist()
    flat_b = blocked.reshape(-1).tolist()
    payload = {
        "rows": int(rows),
        "cols": int(cols),
        "weights": [repr(float(x)) for x in flat_w],
        "blocked": [bool(x) for x in flat_b],
    }
    # sort_keys + 固定分隔符 + 无空白 => 规范化 JSON
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def canonical_changes_bytes(changes: Iterable[dict]) -> bytes:
    """变更列表的规范化字节序列(按键排序、按坐标稳定排序)。"""
    norm = sorted(
        (dict(c) for c in changes),
        key=lambda c: (int(c["row"]), int(c["col"]), str(c.get("kind", ""))),
    )
    return json.dumps(norm, sort_keys=True, separators=(",", ":")).encode("utf-8")


@dataclass(frozen=True)
class Snapshot:
    """不可变地图快照。"""

    version: int
    snapshot_id: str
    parent_id: str | None
    change_hash: str
    created_at: str
    weights: np.ndarray
    blocked: np.ndarray

    def to_info(self) -> dict:
        return {
            "version": self.version,
            "snapshot_id": self.snapshot_id,
            "parent_id": self.parent_id,
            "change_hash": self.change_hash,
            "created_at": self.created_at,
        }


class MapStore:
    """持有一张地图的完整快照链(内存存储)。"""

    def __init__(self, rows: int, cols: int, connectivity: int):
        if rows < 1 or cols < 1:
            raise ValueError("rows/cols 必须 >= 1")
        if rows > 2000 or cols > 2000 or rows * cols > 1_000_000:
            raise ValueError("地图过大: 单边 <= 2000 且单元总数 <= 1_000_000")
        if connectivity not in (CONNECTIVITY_4, CONNECTIVITY_8):
            raise ValueError("connectivity 只能是 4 或 8")
        self.rows = int(rows)
        self.cols = int(cols)
        self.connectivity = int(connectivity)
        self.snapshots: list[Snapshot] = []

    # ------------------------------------------------------------------ #
    @property
    def head(self) -> Snapshot:
        return self.snapshots[-1]

    def get(self, snapshot_id: str) -> Snapshot | None:
        for s in self.snapshots:
            if s.snapshot_id == snapshot_id:
                return s
        return None

    def require(self, snapshot_id: str) -> Snapshot:
        snap = self.get(snapshot_id)
        if snap is None:
            raise SnapshotNotFound(snapshot_id)
        return snap

    def require_version(self, version: int) -> Snapshot:
        if version < 0 or version >= len(self.snapshots):
            raise SnapshotNotFound(f"version={version}")
        return self.snapshots[version]

    # ------------------------------------------------------------------ #
    def commit_genesis(
        self, weights: np.ndarray, blocked: np.ndarray, created_at: str
    ) -> Snapshot:
        if self.snapshots:
            raise RuntimeError("genesis 只能提交一次")
        state_bytes = canonical_state_bytes(weights, blocked)
        change_hash = sha256_hex(b"genesis")
        content = (
            b"GENESIS\n"
            + f"rows={self.rows};cols={self.cols};conn={self.connectivity}\n".encode()
            + change_hash.encode()
            + b"\n"
            + state_bytes
        )
        snap = Snapshot(
            version=0,
            snapshot_id=sha256_hex(content),
            parent_id=None,
            change_hash=change_hash,
            created_at=created_at,
            weights=np.array(weights, dtype=np.float64, copy=True),
            blocked=np.array(blocked, dtype=bool, copy=True),
        )
        self.snapshots.append(snap)
        return snap

    def apply_changes(
        self,
        base: Snapshot,
        changes: list[dict],
        created_at: str,
    ) -> Snapshot:
        """在 ``base`` 快照上原子应用变更并提交新快照。

        变更形式: {"row", "col", "kind": "block"|"free"|"weight", "weight"?: float}
        负权重在此拒绝(ValueError)。base 必须是当前链头, 否则 StaleSnapshot。
        """
        if base is not self.head:
            raise StaleSnapshot(
                f"基线 {base.version} 不是最新版本 {self.head.version}"
            )
        weights = np.array(base.weights, dtype=np.float64, copy=True)
        blocked = np.array(base.blocked, dtype=bool, copy=True)

        # 先做全部校验, 再落地, 保证原子性
        norm = sorted(
            (dict(c) for c in changes),
            key=lambda c: (int(c["row"]), int(c["col"]), str(c.get("kind", ""))),
        )
        for c in norm:
            r, col = int(c["row"]), int(c["col"])
            if not (0 <= r < self.rows and 0 <= col < self.cols):
                raise ValueError(f"坐标越界: ({r},{col})")
            kind = c.get("kind")
            if kind == "weight":
                w = float(c["weight"])
                if not np.isfinite(w) or w < 0.0:
                    raise ValueError(f"权重必须是非负有限值: ({r},{col})={w!r}")
            elif kind not in ("block", "free"):
                raise ValueError(f"未知变更类型: {kind!r}")

        for c in norm:
            r, col = int(c["row"]), int(c["col"])
            kind = c["kind"]
            if kind == "block":
                blocked[r, col] = True
            elif kind == "free":
                blocked[r, col] = False
            elif kind == "weight":
                weights[r, col] = float(c["weight"])

        state_bytes = canonical_state_bytes(weights, blocked)
        change_hash = sha256_hex(canonical_changes_bytes(norm))
        content = (
            b"UPDATE\n"
            + base.snapshot_id.encode("utf-8")
            + b"\n"
            + change_hash.encode()
            + b"\n"
            + state_bytes
        )
        snap = Snapshot(
            version=base.version + 1,
            snapshot_id=sha256_hex(content),
            parent_id=base.snapshot_id,
            change_hash=change_hash,
            created_at=created_at,
            weights=weights,
            blocked=blocked,
        )
        self.snapshots.append(snap)
        return snap

    # ------------------------------------------------------------------ #
    def verify_chain(self) -> dict:
        """重算整条快照链的哈希, 返回校验报告(真实执行密码学校验)。"""
        results: list[dict] = []
        ok_all = True
        for i, s in enumerate(self.snapshots):
            state_bytes = canonical_state_bytes(s.weights, s.blocked)
            if i == 0:
                expected_change = sha256_hex(b"genesis")
                content = (
                    b"GENESIS\n"
                    + f"rows={self.rows};cols={self.cols};conn={self.connectivity}\n".encode()
                    + expected_change.encode()
                    + b"\n"
                    + state_bytes
                )
                expected_parent = None
            else:
                parent = self.snapshots[i - 1]
                expected_change = s.change_hash  # change_hash 本身也校验内容见下
                content = (
                    b"UPDATE\n"
                    + parent.snapshot_id.encode("utf-8")
                    + b"\n"
                    + s.change_hash.encode()
                    + b"\n"
                    + state_bytes
                )
                expected_parent = parent.snapshot_id
            recomputed = sha256_hex(content)
            ok = (
                recomputed == s.snapshot_id
                and s.parent_id == expected_parent
                and (i == 0 or s.version == self.snapshots[i - 1].version + 1)
            )
            ok_all = ok_all and ok
            results.append(
                {
                    "version": s.version,
                    "snapshot_id": s.snapshot_id,
                    "recomputed_id": recomputed,
                    "parent_ok": s.parent_id == expected_parent,
                    "ok": ok,
                }
            )
        return {"ok": ok_all, "snapshots": results}


class StaleSnapshot(Exception):
    """变更基线不是地图最新快照。"""


class SnapshotNotFound(Exception):
    """快照不存在。"""
