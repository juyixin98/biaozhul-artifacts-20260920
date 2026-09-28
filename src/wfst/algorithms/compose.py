"""FST 组合：M1 ∘ M2，带 L*R* epsilon 顺序过滤器。

组合状态按「双侧进度（q1, q2）+ 过滤器状态 f」即时生成，只展开从
初态可达的状态。过滤器保证每条接受对齐恰有一条路径（无重复计数）。
权重在热带半环下相加；终态权重相加。

支持范围：
* 输入/输出 epsilon 分离（见 :mod:`wfst.core.fst`）；
* 允许 epsilon 自环（零/正代价），最短路径按状态去重不会死循环；
* 若组合结果存在负代价环，最短路径层会显式报错（见 cycles 模块）。
"""

from __future__ import annotations

from dataclasses import dataclass

from wfst.algorithms.epsilon_filter import (
    FilterState,
    MoveKind,
    allowed,
    move_kind_left,
    move_kind_right,
    next_state,
)
from wfst.core.fst import EPSILON, FST

ProductKey = tuple[int, int, FilterState]


@dataclass(frozen=True, slots=True)
class _PendingArc:
    """展开产生的一条弧（目标用乘积键描述，尚未分配输出状态号）。"""

    dst: ProductKey
    ilabel: str
    olabel: str
    weight: float


class Composer:
    """增量构建组合机，避免一次性笛卡尔积（只生成可达状态）。"""

    def __init__(self, left: FST, right: FST, name: str | None = None):
        self.left = left
        self.right = right
        self.out = FST(name=name or f"({left.name}∘{right.name})")
        # 右侧按 (src, ilabel) 索引，MATCH 时快速找 b1 == a2 的弧。
        self._right_match: dict[tuple[int, str], list] = {}
        for arc in right.arcs:
            if arc.ilabel != EPSILON:
                self._right_match.setdefault((arc.src, arc.ilabel), []).append(arc)
        self._state_ids: dict[ProductKey, int] = {}

    def run(self) -> FST:
        root: ProductKey = (self.left.start, self.right.start, FilterState.NEUTRAL)
        queue: list[ProductKey] = [root]
        self._state_ids[root] = 0
        # (src_id, dst_key, ilabel, olabel) -> 最小权重，先按乘积键收集，
        # 待所有状态号确定后再落弧。
        parallel: dict[tuple[int, ProductKey, str, str], float] = {}

        head = 0
        while head < len(queue):
            key = queue[head]
            head += 1
            q1, q2, f = key
            sid = self._state_ids[key]

            for pa in self._expand(q1, q2, f):
                # 统一在这里分配目标状态号并入队（保证每个可达键恰好入队一次）。
                dst_id = self._state_ids.get(pa.dst)
                if dst_id is None:
                    dst_id = self.out.new_state()
                    self._state_ids[pa.dst] = dst_id
                    queue.append(pa.dst)
                pkey = (sid, pa.dst, pa.ilabel, pa.olabel)
                prev = parallel.get(pkey)
                # 热带半环：端点/标号完全相同的并行弧取最小代价（不重复计数）。
                if prev is None or pa.weight < prev:
                    parallel[pkey] = pa.weight

        # 终态：两侧同时终态，三种过滤器状态均可结束（epsilon 块可止于接受位）。
        for (q1, q2, _f), sid in self._state_ids.items():
            if q1 in self.left.finals and q2 in self.right.finals:
                w = self.left.finals[q1] + self.right.finals[q2]
                old = self.out.finals.get(sid)
                if old is None or w < old:
                    self.out.finals[sid] = w

        for (src, dst_key, ilabel, olabel), w in parallel.items():
            self.out.add_arc(
                src, self._state_ids[dst_key], ilabel, olabel, w
            )
        return self.out

    def _expand(self, q1: int, q2: int, f: FilterState) -> list[_PendingArc]:
        created: list[_PendingArc] = []

        # ---- MATCH：左侧 olabel 为真实符号，与右侧 ilabel 配对 ----
        for larc in self.left.outgoing(q1):
            if move_kind_left(larc.kind()) is not MoveKind.MATCH:
                continue  # 左侧 ε 输出走 L 分支
            for rarc in self._right_match.get((q2, larc.olabel), ()):
                nf = next_state(f, MoveKind.MATCH)
                created.append(
                    _PendingArc(
                        (larc.dst, rarc.dst, nf),
                        larc.ilabel,
                        rarc.olabel,
                        larc.weight + rarc.weight,
                    )
                )

        # ---- L 走法：左侧单侧推进（olabel == ε）----
        if allowed(f, MoveKind.LEFT_EPS):
            for larc in self.left.outgoing(q1):
                if move_kind_left(larc.kind()) is not MoveKind.LEFT_EPS:
                    continue
                nf = next_state(f, MoveKind.LEFT_EPS)
                created.append(
                    _PendingArc((larc.dst, q2, nf), larc.ilabel, EPSILON, larc.weight)
                )

        # ---- R 走法：右侧单侧推进（ilabel == ε）----
        if allowed(f, MoveKind.RIGHT_EPS):
            for rarc in self.right.outgoing(q2):
                if move_kind_right(rarc.kind()) is not MoveKind.RIGHT_EPS:
                    continue
                nf = next_state(f, MoveKind.RIGHT_EPS)
                created.append(
                    _PendingArc((q1, rarc.dst, nf), EPSILON, rarc.olabel, rarc.weight)
                )

        return created


def compose(left: FST, right: FST, name: str | None = None) -> FST:
    """返回 ``left ∘ right``（先 left 后 right 的关系复合）。"""
    return Composer(left, right, name=name).run()
