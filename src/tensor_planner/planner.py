"""基于最后使用点（last-use）的静态缓冲区复用规划器。

规划对象是 *存储组根*（见 :mod:`tensor_planner.liveness`）：普通值各自成组，
slice 视图并入基底组。规划器在一整块 arena 上为每个存储组分配定长槽位，
当存储组到达死亡时刻（最后一次读取之后）即回收槽位，供同一时刻之后
出生的新张量复用——端点相接（前者 ``end == t``、后者 ``start == t``）
允许复用，因为读取发生在写入之前。

分配策略（确定性、可复现）：
1. 按出生时刻升序处理存储组；
2. 回收死槽后按地址合并相邻空闲段；
3. 从空闲段中选 *最小可容纳* 的一段（best-fit）；放不下则在 arena
   末尾 bump 高水位。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from .dag import DAG
from .liveness import LiveInterval, storage_intervals, value_intervals


@dataclass(frozen=True)
class Placement:
    """一个存储组在 arena 中的放置。"""

    root: str
    offset: int  # 元素偏移
    size: int  # 槽位长度（元素）
    interval: LiveInterval
    members: Tuple[str, ...]  # 共享该槽位的值（基底 + 其切片视图）

    @property
    def end(self) -> int:
        return self.offset + self.size


@dataclass(frozen=True)
class FreeSegment:
    offset: int
    size: int

    @property
    def end(self) -> int:
        return self.offset + self.size


@dataclass
class Plan:
    """内存规划结果。"""

    arena_size: int  # arena 总长度（元素）——即峰值占用
    placements: Dict[str, Placement]
    storage_intervals: Dict[str, LiveInterval]
    # 复用记录：新槽位被哪些根先后使用，按 (offset, 出生时刻) 排序
    reuse_chains: List[Tuple[int, List[str]]]
    # 逐时刻存活字节（元素数）统计：time -> live elements
    live_per_time: Dict[int, int]
    peak_live_elements: int
    peak_time: int

    def placement_of(self, value: str) -> Placement:
        """按值名查询放置（视图值返回其基底的放置）。"""
        root = self.root_of_value[value]
        return self.placements[root]

    root_of_value: Dict[str, str] = field(default_factory=dict)


def _coalesce(segments: List[FreeSegment]) -> List[FreeSegment]:
    """合并地址相邻的空闲段，返回按 offset 排序的列表。"""
    if not segments:
        return []
    ordered = sorted(segments, key=lambda seg: seg.offset)
    merged = [ordered[0]]
    for seg in ordered[1:]:
        last = merged[-1]
        if seg.offset == last.end:
            merged[-1] = FreeSegment(last.offset, last.size + seg.size)
        else:
            merged.append(seg)
    return merged


def _best_fit(
    free: List[FreeSegment], size: int
) -> Tuple[Optional[FreeSegment], List[FreeSegment]]:
    """返回 (选中的段, 分配后剩余的空闲段列表)。选最小可容纳段。"""
    candidates = sorted(
        (seg for seg in free if seg.size >= size),
        key=lambda seg: (seg.size, seg.offset),
    )
    if not candidates:
        return None, free
    chosen = candidates[0]
    rest = [seg for seg in free if seg is not chosen]
    if chosen.size > size:
        rest.append(FreeSegment(chosen.offset + size, chosen.size - size))
    return chosen, _coalesce(rest)


def _events(
    stor_iv: Dict[str, LiveInterval],
) -> Tuple[Dict[int, List[str]], Dict[int, List[str]]]:
    """把存储区间展开为 (出生表, 死亡表)，键为时刻。"""
    births: Dict[int, List[str]] = {}
    deaths: Dict[int, List[str]] = {}
    for root, iv in stor_iv.items():
        births.setdefault(iv.start, []).append(root)
        if iv.end > iv.start:
            deaths.setdefault(iv.end, []).append(root)
    return births, deaths


@dataclass
class _Allocator:
    """事件驱动的 arena 分配器（build_plan 的内部状态机）。"""

    dag: DAG
    stor_iv: Dict[str, LiveInterval]
    placements: Dict[str, Placement] = field(default_factory=dict)
    free_pool: List[FreeSegment] = field(default_factory=list)
    arena_high_water: int = 0
    live_per_time: Dict[int, int] = field(default_factory=dict)
    live_segments: Dict[str, int] = field(default_factory=dict)

    def step(self, time: int, deaths: List[str], births: List[str]) -> None:
        self._reclaim(deaths)
        for root in sorted(births):
            self._allocate(root, time)
        if self.live_segments:
            self.live_per_time[time] = sum(self.live_segments.values())

    def _reclaim(self, deaths: List[str]) -> None:
        """回收在本时刻读完的槽位并合并相邻空闲段。"""
        for root in sorted(deaths):
            slot = self.placements[root]
            self.free_pool.append(FreeSegment(slot.offset, slot.size))
            self.live_segments.pop(root, None)
        self.free_pool = _coalesce(self.free_pool)

    def _allocate(self, root: str, time: int) -> None:
        size = self.dag.storage_size(root)
        chosen, self.free_pool = _best_fit(self.free_pool, size)
        if chosen is None:
            offset = self.arena_high_water
            self.arena_high_water += size
        else:
            offset = chosen.offset
        self.placements[root] = Placement(
            root=root,
            offset=offset,
            size=size,
            interval=self.stor_iv[root],
            members=tuple(sorted(self.dag.members_of(root))),
        )
        if self.stor_iv[root].end > time:  # 零长度区间不纳入存活统计
            self.live_segments[root] = size


def build_plan(dag: DAG) -> Plan:
    """根据最后使用点为 DAG 构造缓冲区复用规划。"""
    val_iv = value_intervals(dag)
    stor_iv = storage_intervals(dag, val_iv)
    births, deaths = _events(stor_iv)

    alloc = _Allocator(dag=dag, stor_iv=stor_iv)
    for t in sorted(set(births) | set(deaths)):
        alloc.step(t, deaths.get(t, []), births.get(t, []))

    live_per_time = alloc.live_per_time
    peak_live = max(live_per_time.values(), default=0)
    peak_time = min(
        (t for t, v in live_per_time.items() if v == peak_live),
        default=0,
    )

    return Plan(
        arena_size=alloc.arena_high_water,
        placements=alloc.placements,
        storage_intervals=stor_iv,
        reuse_chains=_build_reuse_chains(alloc.placements, stor_iv),
        live_per_time=dict(sorted(live_per_time.items())),
        peak_live_elements=peak_live,
        peak_time=peak_time,
        root_of_value=dict(dag.root_of),
    )


def _build_reuse_chains(
    placements: Dict[str, Placement],
    stor_iv: Dict[str, LiveInterval],
) -> List[Tuple[int, List[str]]]:
    """把落在同一 offset 的根按出生顺序串成复用链，只保留被复用的槽位。"""
    by_offset: Dict[int, List[str]] = {}
    for root, slot in placements.items():
        by_offset.setdefault(slot.offset, []).append(root)
    chains: List[Tuple[int, List[str]]] = []
    for offset, roots in by_offset.items():
        ordered = sorted(roots, key=lambda r: (stor_iv[r].start, r))
        if len(ordered) > 1:
            chains.append((offset, ordered))
    return sorted(chains, key=lambda item: (item[0],))


def naive_total_elements(dag: DAG) -> int:
    """不复用基线：为每个值（含视图复制）各分配一块独立缓冲的总元素数。"""
    return sum(dag.num_elements(name) for name in dag.values)


def naive_peak_live_elements(dag: DAG) -> int:
    """不复用基线下的峰值同时存活元素数（值级，视图各计一份）。"""
    val_iv = value_intervals(dag)
    points = sorted(
        {iv.start for iv in val_iv.values()} | {iv.end for iv in val_iv.values()}
    )
    peak = 0
    for t in points:
        total = sum(
            dag.num_elements(name)
            for name, iv in val_iv.items()
            if iv.start <= t < iv.end
        )
        peak = max(peak, total)
    return peak
