"""刚体变换时间树。

结构：以坐标系（帧）为节点、以带时间序列的父子关系为边的有根树。
每条边存 T_parent_child：p_parent = R p_child + t。

查询 T_frameA_frameB（任意两帧、任意时刻）时：
1. 找到最近公共祖先 LCA；
2. 从 A 向上到 LCA，逐边取 T_child_parent（逆变换）连乘；
3. 从 LCA 向下到 B，逐边取 T_parent_child 连乘；
4. 合成为 T_A_B。

拓扑校验：
- add_edge 时若 child 已有不同 parent -> MultipleParentsError；
- 新边的 parent 位于 child 子树内（会成环）-> CycleDetectedError；
- 查询的两帧分属不同根（森林）-> FramesNotConnectedError。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

import numpy as np

from .errors import (
    CycleDetectedError,
    FramesNotConnectedError,
    MultipleParentsError,
    UnknownFrameError,
)
from .timed_sequence import StaticTransformProvider, TimedTransformSequence
from .transform import Transform


@dataclass(frozen=True)
class Edge:
    """父子关系边：parent -> child，附带随时间变化或静态的变换提供者。"""

    parent: str
    child: str
    provider: TimedTransformSequence | StaticTransformProvider


class TransformTree:
    """变换森林（通常所有帧共享一个根）。"""

    def __init__(self) -> None:
        self._parents: dict[str, str] = {}
        # parent -> {child: provider}
        self._children: dict[str, set[str]] = {}
        self._providers: dict[str, TimedTransformSequence | StaticTransformProvider] = {}
        self._frames: set[str] = set()

    @property
    def frames(self) -> set[str]:
        return set(self._frames)

    @property
    def edges(self) -> list[Edge]:
        return [Edge(parent=p, child=c, provider=self._providers[c]) for c, p in self._parents.items()]

    def has_frame(self, frame: str) -> bool:
        return frame in self._frames

    def add_frame(self, frame: str) -> None:
        self._validate_name(frame)
        self._frames.add(frame)
        self._children.setdefault(frame, set())

    def add_edge(
        self,
        parent: str,
        child: str,
        provider: TimedTransformSequence | StaticTransformProvider,
    ) -> None:
        """添加 parent->child 边；检测多父冲突与环。"""
        self._validate_name(parent)
        self._validate_name(child)
        if parent == child:
            raise CycleDetectedError(f"不允许自环：{parent} -> {child}")
        existing = self._parents.get(child)
        if existing is not None and existing != parent:
            raise MultipleParentsError(
                f"坐标系 '{child}' 已有父节点 '{existing}'，不能再挂到 '{parent}' 下（多父冲突）"
            )
        # 若 parent 已经是 child 的后代，则新边成环。
        if child in self._frames and self._is_ancestor(child, parent):
            raise CycleDetectedError(
                f"添加边 {parent} -> {child} 会形成环：'{parent}' 当前是 '{child}' 的后代"
            )
        self.add_frame(parent)
        self.add_frame(child)
        self._parents[child] = parent
        self._children[parent].add(child)
        self._providers[child] = provider

    @staticmethod
    def _validate_name(frame: str) -> None:
        if not isinstance(frame, str) or not frame:
            raise UnknownFrameError(f"坐标系名称必须是非空字符串，实际为 {frame!r}")

    def _is_ancestor(self, maybe_ancestor: str, frame: str) -> bool:
        """maybe_ancestor 是否为 frame 的祖先（含 frame 本身）。"""
        node = frame
        while node in self._parents:
            if node == maybe_ancestor:
                return True
            node = self._parents[node]
        return node == maybe_ancestor

    def _path_to_root(self, frame: str) -> list[str]:
        path = [frame]
        while path[-1] in self._parents:
            path.append(self._parents[path[-1]])
        return path

    def _lca(self, frame_a: str, frame_b: str) -> str:
        ancestors_a = set(self._path_to_root(frame_a))
        node = frame_b
        while node not in ancestors_a:
            if node not in self._parents:
                raise FramesNotConnectedError(
                    f"坐标系 '{frame_a}' 与 '{frame_b}' 分属不同根，之间不存在变换路径"
                )
            node = self._parents[node]
        return node

    def _require_frame(self, frame: str) -> None:
        if frame not in self._frames:
            raise UnknownFrameError(f"未知坐标系：'{frame}'")

    def chain_frames(self, source: str, target: str) -> list[str]:
        """返回 source -> ... -> target 经过的帧序列（含端点）。"""
        self._require_frame(source)
        self._require_frame(target)
        if source == target:
            return [source]
        lca = self._lca(source, target)
        upward = self._path_to_root(source)
        up_part = upward[: upward.index(lca) + 1]  # source ... lca
        down_full = self._path_to_root(target)
        down_part = list(reversed(down_full[: down_full.index(lca)]))  # lca 之后 ... target
        return up_part + down_part

    def lookup_edge_transform(self, parent: str, child: str, timestamp: float, max_gap: float | None = None) -> Transform:
        """查询某条边在指定时刻的 T_parent_child。"""
        if self._parents.get(child) != parent:
            raise UnknownFrameError(f"不存在边 {parent} -> {child}")
        return self._providers[child].lookup(timestamp, max_gap=max_gap)

    def lookup_transform(
        self,
        source: str,
        target: str,
        timestamp: float,
        max_gap: float | None = None,
    ) -> Transform:
        """返回 T_source_target：把点从 target 坐标系变换到 source 坐标系。

        即 p_source = T_source_target @ p_target。source == target 时返回单位变换。
        """
        self._require_frame(source)
        self._require_frame(target)
        if source == target:
            return Transform.identity()
        chain = self.chain_frames(source, target)
        result = Transform.identity()
        # 沿链逐段求 T_{node}_{nxt} 并连乘；上行段对存储的 T_parent_child 取逆。
        for node, nxt in zip(chain, chain[1:]):
            if self._parents.get(nxt) == node:
                # node 是 nxt 的父：存储的正是 T_node_nxt（下行）。
                edge_t = self._providers[nxt].lookup(timestamp, max_gap=max_gap)
            else:
                # nxt 是 node 的父：存储的是 T_nxt_node，取逆得 T_node_nxt（上行）。
                edge_t = self._providers[node].lookup(timestamp, max_gap=max_gap).inverse()
            result = result.multiply(edge_t)
        return result

    def coverage_intersection(self, frames: Iterable[str] | None = None) -> tuple[float, float] | None:
        """路径相关动态边的时间覆盖交集（静态边不限制）。

        frames 给定时只考虑这些帧对应的入边；用于查询前预判整链是否同时有数据。
        """
        selected = self._frames if frames is None else set(frames)
        lo, hi = -np.inf, np.inf
        found_dynamic = False
        for child, provider in self._providers.items():
            if child not in selected:
                continue
            if isinstance(provider, StaticTransformProvider):
                continue
            found_dynamic = True
            lo = max(lo, provider.start_time)
            hi = min(hi, provider.end_time)
        if not found_dynamic:
            return None
        return float(lo), float(hi)
