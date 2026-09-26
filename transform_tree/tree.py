"""带时间序列的刚体变换树。

树中每个坐标系（除根外）恰有一条指向父坐标系的边，边上挂载该
"子坐标系在父坐标系中位姿" 的时间序列。查询任意两坐标系在指定时刻
的相对变换时，沿最近公共祖先路径组合各边在同一时刻的插值结果。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .exceptions import (
    ConnectivityError,
    CycleError,
    MultiParentError,
    UnknownFrameError,
)
from .timeseries import TimeSeries
from .transform import Transform


@dataclass
class _Edge:
    parent: str
    series: TimeSeries = field(default_factory=TimeSeries)


class TransformTree:
    """坐标系变换树：每个子坐标系至多一个父坐标系，不允许环。"""

    def __init__(self) -> None:
        # child -> _Edge(parent, series)
        self._edges: dict[str, _Edge] = {}

    # ------------------------------------------------------------------
    # 拓扑维护
    # ------------------------------------------------------------------
    def add_transform(
        self, parent: str, child: str, time: float, transform: Transform
    ) -> None:
        """向 parent->child 边的时间序列中插入一个样本。

        首次建立边时校验：子坐标系不能已有父坐标系（多父冲突），
        且新边不能形成环。
        """
        self._validate_frame_name(parent)
        self._validate_frame_name(child)
        if parent == child:
            raise CycleError(f"坐标系 {parent!r} 不能作为自身的父坐标系")
        edge = self._edges.get(child)
        if edge is None:
            self._check_no_cycle(parent, child)
            edge = _Edge(parent=parent)
            self._edges[child] = edge
        elif edge.parent != parent:
            raise MultiParentError(
                f"坐标系 {child!r} 已有父坐标系 {edge.parent!r}，"
                f"不能再挂载到 {parent!r}"
            )
        edge.series.insert(time, transform)

    def _check_no_cycle(self, parent: str, child: str) -> None:
        """若 parent 的祖先链上存在 child，则新边 parent->child 会成环。"""
        node: str | None = parent
        while node is not None:
            if node == child:
                raise CycleError(f"新增边 {parent!r} -> {child!r} 会形成拓扑环")
            edge = self._edges.get(node)
            node = edge.parent if edge is not None else None

    @staticmethod
    def _validate_frame_name(name: str) -> None:
        if not isinstance(name, str) or not name:
            raise ValueError(f"坐标系名称必须为非空字符串，得到 {name!r}")

    # ------------------------------------------------------------------
    # 查询
    # ------------------------------------------------------------------
    def frames(self) -> list[str]:
        """返回树中所有坐标系名称。"""
        names = set(self._edges)
        for edge in self._edges.values():
            names.add(edge.parent)
        return sorted(names)

    def parent_of(self, frame: str) -> str | None:
        edge = self._edges.get(frame)
        return edge.parent if edge is not None else None

    def lookup_transform(
        self, target: str, source: str, time: float
    ) -> Transform:
        """查询 target_T_source：把 source 坐标系的点映射到 target 坐标系。

        路径上每条边都在同一时刻插值；任一边在该时刻无覆盖则抛
        ExtrapolationError。
        """
        self._validate_frame_name(target)
        self._validate_frame_name(source)
        self._require_known(target)
        self._require_known(source)
        if target == source:
            return Transform.identity()

        # 从 source 沿父指针向根累积：chain[node] = node_T_source
        source_chain: dict[str, Transform] = {source: Transform.identity()}
        child = source
        while child in self._edges:
            edge = self._edges[child]
            sample = edge.series.interpolate(time)  # parent_T_child
            parent = edge.parent
            source_chain[parent] = sample.compose(source_chain[child])
            child = parent

        # 从 target 沿父指针向上，找第一个出现在 source_chain 中的祖先
        target_acc = Transform.identity()  # node_T_target
        node = target
        while True:
            if node in source_chain:
                # target_T_source = inverse(node_T_target) ∘ (node_T_source)
                return target_acc.inverse().compose(source_chain[node])
            edge = self._edges.get(node)
            if edge is None:
                break
            sample = edge.series.interpolate(time)  # parent_T_node
            target_acc = sample.compose(target_acc)
            node = edge.parent

        raise ConnectivityError(
            f"坐标系 {target!r} 与 {source!r} 之间不存在连通路径"
        )

    def _require_known(self, frame: str) -> None:
        if frame not in self._edges and not any(
            e.parent == frame for e in self._edges.values()
        ):
            raise UnknownFrameError(f"坐标系 {frame!r} 不在变换树中")
