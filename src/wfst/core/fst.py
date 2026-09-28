"""核心层：FST 数据结构、符号约定与代价算子。

设计约定（明确的支持范围）
--------------------------
* 输入 epsilon 与输出 epsilon 是**两个不同的符号**：
    - 输入 epsilon ``<eps>``：弧不消耗任何输入字符；
    - 输出 epsilon ``<eps>``：弧不发射任何输出字符。
  二者在组合时通过三态 epsilon 过滤器（状态 0/1/2）区分，
  因此「只吃输入不产出」（<eps-in> 等待）与「只产出不吃输入」
  （<eps-out> 等待）不会被混淆，也不会对同一条路径重复计数。
* 权重采用**热带半环（tropical semiring）**：组合时代价相加，
  同一目标状态的并行路径取最小值。
* 代价均为有限实数。算法仅对代价非负（允许零代价自环）的图保证
  最短路径正确；对含负代价弧的图，:func:`detect_negative_cycle` 显式
  检测负代价环，最短路径在发现负环时抛出
  :class:`~wfst.core.errors.NegativeCycleError`，绝不静默返回成功。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

EPSILON = "<eps>"
"""输入/输出共用的 epsilon 字面量；靠弧上的位置（ilabel/olabel）区分语义。"""

# 无穷代价（Python float 可表示的最大有限值的一半，相加不会溢出 inf）。
INFINITY = float(1e308) / 4.0


class AlignmentError(ValueError):
    """输入串无法被转换器接受（无任何从初态到终态的消费路径）。"""


class NegativeCycleError(RuntimeError):
    """图中存在从可达结构出发的负代价环，最短路径无定义（可无限下降）。"""

    def __init__(self, cycle_states: Iterable[int]):
        self.cycle_states = tuple(cycle_states)
        super().__init__(
            f"检测到负代价环，涉及状态 {self.cycle_states}；"
            "热带半环下最短路径不存在"
        )


class CompositionError(ValueError):
    """组合过程中出现不可满足的结构（如过滤器状态非法）。"""


class ArcKind(str, Enum):
    """弧按 epsilon 参与方式的四分类，供组合过滤器使用。"""

    NORMAL = "normal"          # 真实输入符号 : 真实输出符号
    EPS_IN = "eps_in"          # 输入 epsilon : 真实输出（只产出，等待右侧吃入）
    EPS_OUT = "eps_out"        # 真实输入 : 输出 epsilon（只吃输入）
    EPS_EPS = "eps_eps"        # 双 epsilon（自由跳转，两侧都不推进）


@dataclass(frozen=True, slots=True)
class Arc:
    """一条加权弧。ilabel/olabel 等于 :data:`EPSILON` 时表示该侧 epsilon。"""

    src: int
    dst: int
    ilabel: str
    olabel: str
    weight: float = 0.0

    def kind(self) -> ArcKind:
        ie = self.ilabel == EPSILON
        oe = self.olabel == EPSILON
        if ie and oe:
            return ArcKind.EPS_EPS
        if ie:
            return ArcKind.EPS_IN
        if oe:
            return ArcKind.EPS_OUT
        return ArcKind.NORMAL


@dataclass(frozen=True, slots=True)
class FinalState:
    """终态及其终止权重（终止代价）。"""

    state: int
    weight: float = 0.0


@dataclass
class FST:
    """可变的有限状态转换器（构建期），构建完成后按只读使用。

    状态用连续整数 0..n-1 表示；``start`` 为唯一初态。
    """

    name: str = "fst"
    start: int = 0
    arcs: list[Arc] = field(default_factory=list)
    finals: dict[int, float] = field(default_factory=dict)
    input_symbols: set[str] = field(default_factory=set)
    output_symbols: set[str] = field(default_factory=set)
    _state_count: int = 1

    # ---- 构建 ----
    def new_state(self) -> int:
        s = self._state_count
        self._state_count += 1
        cache = getattr(self, _OUT_INDEX_ATTR, None)
        if cache is not None:
            cache.append([])
        return s

    def add_arc(
        self,
        src: int,
        dst: int,
        ilabel: str,
        olabel: str,
        weight: float = 0.0,
    ) -> Arc:
        if ilabel != EPSILON:
            self.input_symbols.add(ilabel)
        if olabel != EPSILON:
            self.output_symbols.add(olabel)
        arc = Arc(src, dst, ilabel, olabel, float(weight))
        self.arcs.append(arc)
        # 允许弧直接引用尚未显式创建的状态：按需扩展状态空间。
        needed = max(src, dst) + 1
        if needed > self._state_count:
            self._state_count = needed
            cache = getattr(self, _OUT_INDEX_ATTR, None)
            if cache is not None:  # 索引已建立（构建期追加弧的少见情形）
                cache.extend(
                    [] for _ in range(needed - len(cache))
                )
        cache = getattr(self, _OUT_INDEX_ATTR, None)
        if cache is not None:
            cache[src].append(arc)
        return arc

    def set_final(self, state: int, weight: float = 0.0) -> None:
        needed = state + 1
        if needed > self._state_count:
            self._state_count = needed
            cache = getattr(self, _OUT_INDEX_ATTR, None)
            if cache is not None:
                cache.extend([] for _ in range(needed - len(cache)))
        self.finals[state] = float(weight)

    # ---- 查询 ----
    @property
    def num_states(self) -> int:
        return self._state_count

    def outgoing(self, src: int) -> list[Arc]:
        return _out_index(self)[src]

    def is_final(self, state: int) -> bool:
        return state in self.finals

    def final_weight(self, state: int) -> float:
        return self.finals.get(state, INFINITY)

    def total_weight(self) -> float:
        """所有弧代价与终态权重之和，用于健全性自检。"""
        return sum(a.weight for a in self.arcs) + sum(self.finals.values())

    def describe(self) -> str:
        return (
            f"FST({self.name!r}, states={self.num_states}, "
            f"arcs={len(self.arcs)}, finals={sorted(self.finals)}, "
            f"start={self.start})"
        )


# ---------- 延迟建立的出弧索引 ----------

_OUT_INDEX_ATTR = "_out_index_cache"


def _out_index(fst: FST) -> list[list[Arc]]:
    cache = getattr(fst, _OUT_INDEX_ATTR, None)
    if cache is None:
        index: list[list[Arc]] = [[] for _ in range(fst.num_states)]
        for arc in fst.arcs:
            index[arc.src].append(arc)
        object.__setattr__(fst, _OUT_INDEX_ATTR, index)
        cache = index
    return cache
