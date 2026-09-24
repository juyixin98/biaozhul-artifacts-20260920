"""带时间戳的 TF 树。

- :class:`TimeCache`：单条父子边上的带时间戳 SE3 采样序列，平移线性插值、
  旋转 SLERP，拒绝范围外的外推。
- :class:`TransformTree`：无向树（有向边语义），支持任意两个已连通坐标系之间
  的多边组合查询与逆变换；添加会成环的边时抛出 :class:`TFCycleError`。

语义约定：``add_transform(parent, child, t, T)`` 写入的 T 是 *child 在 parent
中的位姿*，即 ``p_parent = T @ p_child``（记作 T_parent_child）。
``lookup_transform(source, target, t)`` 返回 T_target_source，即
``p_target = T @ p_source``。
"""

from __future__ import annotations

import threading
from collections import deque
from typing import Iterable

import numpy as np

from .errors import (
    DuplicateTimestampError,
    ExtrapolationNotAllowedError,
    FrameNotFoundError,
    TFCycleError,
)
from .se3 import SE3Transform

# 时间戳等于已有采样 / 边界时的吸附容差（秒）。
_TIME_SNAP = 1e-9
# 相邻采样时间戳必须为正且超过该值，避免除零。
_DT_EPS = 1e-12


class TimeCache:
    """一条边上的时间戳变换缓存（采样按时间升序保存）。"""

    def __init__(self, frame_a: str, frame_b: str):
        self.frame_a = frame_a
        self.frame_b = frame_b
        self._times: list[float] = []
        self._trans: list[np.ndarray] = []
        self._quats: list[np.ndarray] = []

    # ---- 写入 ----------------------------------------------------------
    def add_sample(self, t: float, transform: SE3Transform) -> None:
        t = float(t)
        if not np.isfinite(t):
            raise ValueError("时间戳必须是有限数")

        times = self._times
        if times and t <= times[-1] + _TIME_SNAP:
            # 只允许严格递增；与已有采样重合视为重复
            idx = int(np.searchsorted(times, t))
            close = (
                idx < len(times) and abs(times[idx] - t) <= _TIME_SNAP
            ) or (idx > 0 and abs(times[idx - 1] - t) <= _TIME_SNAP)
            if close:
                raise DuplicateTimestampError(
                    f"边 {self.frame_a}->{self.frame_b} 在 t={t} 已存在采样"
                )
            raise ValueError(
                f"边 {self.frame_a}->{self.frame_b} 的采样必须按时间递增写入："
                f"新采样 t={t} 不晚于末值 {times[-1]}"
            )

        self._times.append(t)
        self._trans.append(transform.translation.copy())
        self._quats.append(transform.quaternion.copy())

    # ---- 查询 ----------------------------------------------------------
    @property
    def times(self) -> tuple[float, ...]:
        return tuple(self._times)

    def __len__(self) -> int:
        return len(self._times)

    def time_bounds(self) -> tuple[float, float] | None:
        if not self._times:
            return None
        return self._times[0], self._times[-1]

    def has_coverage(self, t: float) -> bool:
        if not self._times:
            return False
        return self._times[0] - _TIME_SNAP <= t <= self._times[-1] + _TIME_SNAP

    def lookup(self, t: float) -> SE3Transform:
        """返回 t 时刻的变换（端点吸附 / 区间内线性 + SLERP）。

        超出采样时间范围一律抛出 :class:`ExtrapolationNotAllowedError`，
        本服务不做外推。
        """
        if not self._times:
            raise ExtrapolationNotAllowedError(
                f"边 {self.frame_a}->{self.frame_b} 没有任何采样"
            )
        t = float(t)
        first, last = self._times[0], self._times[-1]
        if t < first - _TIME_SNAP or t > last + _TIME_SNAP:
            raise ExtrapolationNotAllowedError(
                f"边 {self.frame_a}->{self.frame_b} 请求 t={t:.9g} 超出范围 "
                f"[{first:.9g}, {last:.9g}]，拒绝外推"
            )

        # 端点吸附
        if abs(t - first) <= _TIME_SNAP:
            return SE3Transform(self._trans[0], self._quats[0])
        if abs(t - last) <= _TIME_SNAP:
            return SE3Transform(self._trans[-1], self._quats[-1])

        i = int(np.searchsorted(self._times, t))
        # 与内部采样重合
        if i < len(self._times) and abs(self._times[i] - t) <= _TIME_SNAP:
            return SE3Transform(self._trans[i], self._quats[i])

        t0, t1 = self._times[i - 1], self._times[i]
        dt = t1 - t0
        if dt < _DT_EPS:  # 理论不可达（写入时保证严格递增），防御性检查
            raise RuntimeError(f"相邻采样时间戳过于接近：{t0}, {t1}")
        alpha = (t - t0) / dt
        a = SE3Transform(self._trans[i - 1], self._quats[i - 1])
        b = SE3Transform(self._trans[i], self._quats[i])
        return SE3Transform.interpolate(a, b, alpha)


class TransformTree:
    """离线 TF 树：保存多条边的时间缓存，组合查询任意连通坐标系。"""

    def __init__(self):
        # frozenset({a, b}) -> TimeCache（无向边标识）
        self._edges: dict[frozenset[str], TimeCache] = {}
        # frozenset -> (parent, child)：写入时的有向语义
        self._edge_orientation: dict[frozenset[str], tuple[str, str]] = {}
        self._frames: set[str] = set()
        self._lock = threading.RLock()

    # ---- 写入 ----------------------------------------------------------
    def add_transform(
        self, parent: str, child: str, t: float, transform: SE3Transform
    ) -> None:
        if not isinstance(parent, str) or not isinstance(child, str):
            raise TypeError("坐标系名称必须是字符串")
        if parent == child:
            raise TFCycleError(f"不允许自环：{parent} -> {child}")

        with self._lock:
            key = frozenset((parent, child))
            existing = self._edges.get(key)
            if existing is not None:
                # 同一条边：按反方向写入时自动取逆
                orient_a, orient_b = self._edge_orientation[key]
                if (parent, child) == (orient_b, orient_a):
                    transform = transform.inverse()
                existing.add_sample(t, transform)
                return

            # 新边：若两个端点都已存在且已经连通，则必然成环
            if parent in self._frames and child in self._frames:
                if self._connected(parent, child):
                    raise TFCycleError(
                        f"添加边 {parent}->{child} 会形成环：两坐标系已连通"
                    )

            cache = TimeCache(parent, child)
            cache.add_sample(t, transform)
            self._edges[key] = cache
            self._edge_orientation[key] = (parent, child)
            self._frames.add(parent)
            self._frames.add(child)

    def add_transforms(
        self, parent: str, child: str, samples: Iterable[tuple[float, SE3Transform]]
    ) -> None:
        """批量写入一条边的采样，必须按时间递增。"""
        for t, tf in samples:
            self.add_transform(parent, child, t, tf)

    # ---- 查询 ----------------------------------------------------------
    @property
    def frames(self) -> set[str]:
        with self._lock:
            return set(self._frames)

    def edges(self) -> list[tuple[str, str, tuple[float, float] | None]]:
        """返回 [(parent, child, (t_min, t_max)|None), ...]，用于信息展示。"""
        with self._lock:
            out = []
            for key in self._edges:
                a, b = self._edge_orientation[key]
                out.append((a, b, self._edges[key].time_bounds()))
            return sorted(out, key=lambda x: (x[0], x[1]))

    def has_frame(self, frame: str) -> bool:
        return frame in self._frames

    def lookup_transform(
        self, source: str, target: str, t: float
    ) -> SE3Transform:
        """求 t 时刻 source -> target 的组合变换 T_target_source。

        - source == target：返回单位变换（帧必须存在，或两者同名时允许）。
        - 帧不存在 / 链不连通：抛出 FrameNotFoundError。
        - 链上任一边在 t 时刻无覆盖：抛出 ExtrapolationNotAllowedError。
        """
        with self._lock:
            path = self.find_path(source, target)
            if len(path) == 1:
                return SE3Transform.identity()

            result = SE3Transform.identity()
            for a, b in zip(path[:-1], path[1:]):
                key = frozenset((a, b))
                cache = self._edges[key]
                edge_tf = cache.lookup(t)  # T_parent_child（写入方向）
                parent, child = self._edge_orientation[key]
                if (a, b) == (parent, child):
                    # 沿 parent -> child 行走：需要 T_child_parent = 逆变换
                    step = edge_tf.inverse()
                else:
                    # 沿 child -> parent 行走：需要 T_parent_child = 原值
                    step = edge_tf
                result = step.multiply(result)
            return result

    def lookup_transforms(
        self, source: str, target: str, times: Iterable[float]
    ) -> list[SE3Transform]:
        """同一链条上多个时刻的批量查询（用于异步采样场景）。"""
        with self._lock:
            path = self.find_path(source, target)  # 先固定路径，再逐时刻查询
            return [self._lookup_along_path(path, t) for t in times]

    def find_path(self, source: str, target: str) -> list[str]:
        """无向 BFS 最短路径，帧缺失或不连通时抛错。"""
        if source == target:
            if source not in self._frames:
                raise FrameNotFoundError(f"未知坐标系：{source!r}")
            return [source]
        for name in (source, target):
            if name not in self._frames:
                raise FrameNotFoundError(f"未知坐标系：{name!r}")
        if not self._connected(source, target):
            raise FrameNotFoundError(
                f"坐标系 {source!r} 与 {target!r} 之间不存在连通链"
            )

        # BFS 记录前驱
        prev: dict[str, str | None] = {source: None}
        q: deque[str] = deque([source])
        while q:
            cur = q.popleft()
            if cur == target:
                break
            for key in self._edges:
                if cur in key:
                    nxt = next(iter(key - {cur}))
                    if nxt not in prev:
                        prev[nxt] = cur
                        q.append(nxt)
        path: list[str] = []
        node: str | None = target
        while node is not None:
            path.append(node)
            node = prev[node]
        path.reverse()
        return path

    # ---- 内部 ----------------------------------------------------------
    def _connected(self, a: str, b: str) -> bool:
        """无向连通性（BFS），调用方需持锁。"""
        seen = {a}
        q: deque[str] = deque([a])
        while q:
            cur = q.popleft()
            if cur == b:
                return True
            for key in self._edges:
                if cur in key:
                    nxt = next(iter(key - {cur}))
                    if nxt not in seen:
                        seen.add(nxt)
                        q.append(nxt)
        return False

    def _lookup_along_path(self, path: list[str], t: float) -> SE3Transform:
        """沿给定路径在 t 时刻做组合，调用方需持锁。"""
        if len(path) == 1:
            return SE3Transform.identity()
        result = SE3Transform.identity()
        for a, b in zip(path[:-1], path[1:]):
            key = frozenset((a, b))
            cache = self._edges[key]
            edge_tf = cache.lookup(t)
            parent, child = self._edge_orientation[key]
            if (a, b) == (parent, child):
                step = edge_tf.inverse()
            else:
                step = edge_tf
            result = step.multiply(result)
        return result
