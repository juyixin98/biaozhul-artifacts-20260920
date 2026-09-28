"""组合用 epsilon 顺序过滤器（epsilon sequencing filter）。

为什么需要过滤器
----------------
组合 M1∘M2 时，M1 的输出 epsilon 弧（L 走法，只动左侧）与 M2 的输入
epsilon 弧（R 走法，只动右侧）互不干涉、彼此可交换：连续一段 L/R 走法
以不同顺序执行会到达**同一个乘积状态**且代价相同，朴素展开因此对同一
条对齐重复计数。

本过滤器在每个「真实符号匹配（M）分隔的 epsilon 块」内强制规范序
``L* R*``：

====  =========  ==========  ======
当前  允许 L?    允许 R?     允许 M?
====  =========  ==========  ======
0     是 → 1     是 → 2      是 → 0
1     是 → 1     是 → 2      是 → 0
2     否（阻塞） 是 → 2      是 → 0
====  =========  ==========  ======

完备性：L 走法只改变 q1、R 走法只改变 q2，同一块内任意交错序列所用弧
的多重集与 L*R* 重排完全相同，故凡有路必有规范序路径（例如
``a:ε`` 删除弧接 ``ε:b`` 插入弧，``a→b`` 合法，过滤器放行 L 后再 R）。
唯一性：L*R* 排列唯一，且 R 后阻塞 L，故每条对齐恰有一条路径，
不会重复计数。三个过滤器状态均为终态：epsilon 块可以结束在接受位置。

双 epsilon（ε:ε）弧按其所在机器单边处理：M1 的 ε:ε 弧是 L 走法，
M2 的 ε:ε 弧是 R 走法。
"""

from __future__ import annotations

from enum import IntEnum

from wfst.core.fst import ArcKind


class FilterState(IntEnum):
    NEUTRAL = 0  # 块首，尚未见 L/R
    LEFT_RUN = 1  # 本块已走 L（此后 L 可继续，也允许一次性切入 R 段）
    RIGHT_RUN = 2  # 本块已走 R（此后禁止 L）


class MoveKind(IntEnum):
    """乘积展开中的一步。"""

    MATCH = 0       # 真实符号 b1 == a2，两侧同时推进，块结束
    LEFT_EPS = 1    # M1 单侧：其输出为 epsilon
    RIGHT_EPS = 2   # M2 单侧：其输入为 epsilon


def move_kind_left(kind: ArcKind) -> MoveKind:
    """按 M1 弧的**输出侧**判定走法类型（olabel==ε 即 M1 单侧 L 走法）。"""
    if kind in (ArcKind.EPS_OUT, ArcKind.EPS_EPS):
        return MoveKind.LEFT_EPS
    return MoveKind.MATCH


def move_kind_right(kind: ArcKind) -> MoveKind:
    """按 M2 弧的**输入侧**判定（ilabel==ε 即 M2 单侧 R 走法）。"""
    if kind in (ArcKind.EPS_IN, ArcKind.EPS_EPS):
        return MoveKind.RIGHT_EPS
    return MoveKind.MATCH


def allowed(f: FilterState, move: MoveKind) -> bool:
    """过滤器当前状态是否允许该走法。"""
    if move is MoveKind.MATCH:
        return True
    if move is MoveKind.LEFT_EPS:
        return f is not FilterState.RIGHT_RUN
    return True  # RIGHT_EPS 在任意状态都允许（NEUTRAL/LEFT_RUN→2，RIGHT_RUN 保持）


def next_state(f: FilterState, move: MoveKind) -> FilterState:
    """走法后的过滤器状态。"""
    if move is MoveKind.MATCH:
        return FilterState.NEUTRAL
    if move is MoveKind.LEFT_EPS:
        return FilterState.LEFT_RUN
    return FilterState.RIGHT_RUN
