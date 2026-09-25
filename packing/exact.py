"""小规模精确求解器：子集和坐标 + MRV 位掩码 DFS（完备）。

用途与范围
==========
仅用于 *小* 整数算例（n <= :data:`EXACT_MAX_RECTS`，条带宽 <=
:data:`EXACT_MAX_COORD`），在自动化测试中与独立的网格暴力法互相印证，
并给出真实最优高度以衡量启发式差距。**不用于常规请求路径。**

完备性依据（子集和坐标）
========================
判定给定高度 H 是否可排布时，给每件矩形尝试的左下角坐标取自：

* x ∈ {矩形宽度的子集和} ∩ [0, W - w_i]
* y ∈ {矩形高度的子集和} ∩ [0, H - h_i]

取任意可行布局，沿水平方向从条带左边 0 扫描竖边：每条内部竖边都可
解释为一串首尾相接的矩形宽度之和，故每件矩形左边 x_i 必为若干矩形宽度
之和；下底边 y_i 同理。因此"存在可行布局 ⇒ 存在每件左下角都在
子集和坐标上的可行布局"，枚举这些坐标即完备，与放置顺序无关，也允许
布局中存在空洞（条带装箱不要求铺满）。

注意：不能再额外要求"每件必须贴左/下支撑"或"必须覆盖最低最左空格"
——这两类剪枝在允许空洞时都会误删可行解（一个宽矩形可能浮在一个
无法填补的小空洞上方）。本实现只使用以下 *不影响完备性* 的加速：

1. 位掩码网格（W <= 40，一行一个 64 位整数），碰撞 = h_i 次位与；
2. 相同尺寸矩形不可区分：同一节点处同形状只试一次（产生的网格状态相同）；
3. MRV：每步选择可放置位置最少的剩余矩形先放（0 个位置立即剪枝）；
4. 记忆化（剩余件形状多重集 + 网格状态）；
5. 面积剪枝。

最优高度：可行性对 H 单调，在 [max(ceil(面积/W), 最大件高), sum(h)]
内二分搜索。超过 :data:`NODE_BUDGET` 时保守返回 skipped，绝不把未证实的
结果称为最优。
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from .bounds import EXACT_MAX_COORD, EXACT_MAX_RECTS, Instance
from .geometry import EPS

MAX_GRID_WIDTH = 64
NODE_BUDGET = 3_000_000


class _BudgetExhausted(Exception):
    pass


@dataclass
class ExactResult:
    height: float | None
    x: np.ndarray | None
    y: np.ndarray | None
    status: str                # "optimal" | "infeasible" | "skipped"
    reason: str = ""


def _check_integer_small(inst: Instance) -> str | None:
    if inst.n > EXACT_MAX_RECTS:
        return "n=%d 超过精确求解上限 %d" % (inst.n, EXACT_MAX_RECTS)
    if inst.strip_width > EXACT_MAX_COORD + EPS:
        return "strip_width 超过精确求解坐标上限 %d" % EXACT_MAX_COORD
    if float(np.max(inst.widths)) > EXACT_MAX_COORD + EPS or \
            float(np.max(inst.heights)) > EXACT_MAX_COORD + EPS:
        return "矩形尺寸超过精确求解坐标上限 %d" % EXACT_MAX_COORD
    if np.any(np.abs(inst.widths - np.rint(inst.widths)) > 1e-6) or \
            np.any(np.abs(inst.heights - np.rint(inst.heights)) > 1e-6):
        return "精确求解仅支持整数尺寸（位网格按单位格枚举）"
    if int(round(inst.strip_width)) > MAX_GRID_WIDTH:
        return "位网格要求 strip_width <= %d" % MAX_GRID_WIDTH
    return None


def _subset_sums_int(values: list[int]) -> list[int]:
    """非负整数的所有不同子集和（含 0），升序。0/1 背包 DP。"""
    total = sum(values)
    reachable = np.zeros(total + 1, dtype=bool)
    reachable[0] = True
    for v in values:
        reachable[v:] |= reachable[:-v].copy()
    return [int(v) for v in np.nonzero(reachable)[0]]


def _feasible(inst: Instance, H: int, x_sums: list[int],
               y_sums: list[int], shapes: list[tuple[int, int]],
               counts: dict[tuple[int, int], int],
               recover: dict | None = None) -> tuple[bool, bool]:
    """MRV + 位掩码 + 记忆化的 DFS。shapes 为去重后的形状列表。

    counts: 每种 (w, h) 形状的剩余件数。矩形只按形状区分（可行性问题中
    同尺寸件不可区分）；recover 非空时额外记录一种成功放置。
    """
    W = int(round(inst.strip_width))
    n = inst.n
    full_row = (1 << W) - 1
    rows = [0] * H

    # 每种形状的候选 (x, 行位掩码, y) 列表
    cand: dict[tuple[int, int], list[tuple[int, int, int]]] = {}
    for (a, b) in shapes:
        mask_by_x = [((1 << a) - 1) << x for x in x_sums if x + a <= W]
        ys = [y for y in y_sums if y + b <= H]
        cand[(a, b)] = [(x, mask, y) for y in ys for x, mask in
                         zip([v for v in x_sums if v + a <= W], mask_by_x)]

    total_area = sum(a * b * c for (a, b), c in counts.items())
    memo: set[tuple] = set()
    nodes = 0
    placement: list[tuple[int, int, int]] = []  # (shape_index, x, y) 供恢复

    def legal_moves(a: int, b: int) -> list[tuple[int, int, int]]:
        res = []
        for x, mask, y in cand[(a, b)]:
            for t in range(b):
                if rows[y + t] & mask:
                    break
            else:
                res.append((x, mask, y))
        return res

    def state_key() -> tuple:
        # 剩余形状计数（按 shapes 顺序）+ 完整网格
        return tuple(counts[s] for s in shapes) + tuple(rows)

    def dfs(placed_area: int, remaining: int) -> bool:
        nonlocal nodes
        if remaining == 0:
            return True
        nodes += 1
        if nodes > NODE_BUDGET:
            raise _BudgetExhausted

        key = state_key()
        if key in memo:
            return False
        if placed_area > W * H:
            return False

        # MRV：找候选合法位置最少的剩余形状
        best_shape = None
        best_moves: list[tuple[int, int, int]] | None = None
        for s in shapes:
            c = counts[s]
            if c == 0:
                continue
            a, b = s
            moves = legal_moves(a, b)
            if not moves:
                memo.add(key)
                return False
            if best_moves is None or len(moves) < len(best_moves):
                best_shape, best_moves = s, moves
                if len(moves) == 1:
                    break
        assert best_shape is not None and best_moves is not None

        a, b = best_shape
        counts[best_shape] -= 1
        for x, mask, y in best_moves:
            for t in range(b):
                rows[y + t] |= mask
            placement.append((shapes.index(best_shape), x, y))
            if dfs(placed_area + a * b, remaining - 1):
                return True
            placement.pop()
            for t in range(b):
                rows[y + t] &= ~mask
        counts[best_shape] += 1
        memo.add(key)
        return False

    try:
        ok = dfs(0, n)
    except _BudgetExhausted:
        return False, True
    if ok and recover is not None:
        _record_layout(inst, H, shapes, placement, recover)
    return ok, False


def _record_layout(inst: Instance, H: int, shapes: list[tuple[int, int]],
                    placement: list[tuple[int, int, int]], recover: dict) -> None:
    """把按形状记录的放置翻译回每个矩形 id 的坐标（同形状件任意分配）。"""
    n = inst.n
    wi = [int(round(v)) for v in inst.widths]
    hi = [int(round(v)) for v in inst.heights]
    free_ids: dict[tuple[int, int], list[int]] = {}
    for i in range(n):
        free_ids.setdefault((wi[i], hi[i]), []).append(i)
    x_out = np.zeros(n, dtype=np.float64)
    y_out = np.zeros(n, dtype=np.float64)
    for si, x, y in placement:
        rid = free_ids[shapes[si]].pop()
        x_out[rid] = float(x)
        y_out[rid] = float(y)
    recover["x_out"] = x_out
    recover["y_out"] = y_out


def solve_exact(inst: Instance) -> ExactResult:
    """求最小可行整数高度（真实最优）。超规模/超预算返回 ``status='skipped'``。"""
    reason = _check_integer_small(inst)
    if reason is not None:
        return ExactResult(None, None, None, "skipped", reason)

    wi = [int(round(v)) for v in inst.widths]
    hi = [int(round(v)) for v in inst.heights]
    n = inst.n
    W = int(round(inst.strip_width))
    total_area = sum(wi[i] * hi[i] for i in range(n))

    raw_shapes: dict[tuple[int, int], int] = {}
    for i in range(n):
        raw_shapes[(wi[i], hi[i])] = raw_shapes.get((wi[i], hi[i]), 0) + 1
    # 大件形状优先（MRV 内部决定每步选谁，这里顺序只影响遍历与恢复的稳定性）
    shapes = sorted(raw_shapes.keys(), key=lambda s: (-(s[0] * s[1]), -s[1], s[0]))
    counts = dict(raw_shapes)

    h_sums = _subset_sums_int(hi)
    x_sums = _subset_sums_int(wi)

    lo = max(int(np.ceil(total_area / W - 1e-9)), max(hi))
    hi_sum = sum(hi)

    def run(H: int, rec: dict | None = None) -> tuple[bool, bool]:
        y_sums = [v for v in h_sums if v <= H]
        return _feasible(inst, H, x_sums, y_sums, shapes, dict(counts), recover=rec)

    if not run(hi_sum)[0]:
        return ExactResult(None, None, None, "infeasible",
                          "无可行高度（不应出现，请检查输入）")
    if run(lo)[0]:
        rec: dict = {}
        run(lo, rec)
        return ExactResult(float(lo), rec["x_out"], rec["y_out"], "optimal")

    low, high = lo + 1, hi_sum
    last_rec: dict | None = None
    while low < high:
        mid = (low + high) // 2
        rec_mid: dict = {}
        ok, exhausted = run(mid, rec_mid)
        if exhausted:
            return ExactResult(
                None, None, None, "skipped",
                "精确搜索在 H=%d 处超过节点预算 %d（保守停止，不声称最优）"
                % (mid, NODE_BUDGET),
            )
        if ok:
            high = mid
            last_rec = rec_mid
        else:
            low = mid + 1
    if last_rec is None:
        rec = {}
        ok2, exhausted2 = run(low, rec)
        if exhausted2 or not ok2:
            return ExactResult(None, None, None,
                             "skipped" if exhausted2 else "infeasible",
                             "精确求解失败")
        last_rec = rec
    return ExactResult(float(low), last_rec["x_out"], last_rec["y_out"], "optimal")
