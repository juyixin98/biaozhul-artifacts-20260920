"""0/1 背包的分支定界精确求解器（核心算法自行实现）。

算法概述
========
1. **预处理**（全部可证明安全）：
   * 重量 w_i < 0 的实例被输入校验拒绝；w_i = 0 时：
       - v_i >= 0：直接取（不占容量，价值非负）；
       - v_i <  0：直接舍。
     二者都不进入后续搜索。
   * w_i > C（初始容量）的物品在任何可行解中都装不下，直接剔除。
   * 剩余物品按密度 v_i/w_i 降序排列（交叉相乘精确比较，同密度按原
     始索引升序，保证结果确定）。

2. **初始可行解**：密度贪心（0/1，整件装）+ “单件最优”修正，取较好者。
    incumbent 始终是真正可行的解，因此任何时刻返回它都合法。

3. **分支定界 DFS**：节点保存 (深度, 剩余容量, 当前价值, 选取位掩码)。
   * 上界 = 当前价值 + 后缀分数背包松弛（:mod:`knapsack.bounds`），
     用精确整数 floor 剪枝：``bound <= incumbent`` 即剪；
   * 先探“取”分支再探“舍”分支，尽快抬高 incumbent；
   * 超时机制：在根节点及每 ``DEADLINE_CHECK_EVERY`` 个被展开节点处
     检查挂钟截止时间。超时后遍历当前 DFS 栈中未处理的分支节点，取
     其已有上界的最大值，作为剩余解空间的有效上界（这些节点的上界
     在入栈时已精确计算）。

4. **失败状态**：``status`` 只可能是 ``"optimal"`` 或 ``"timeout"``。
   超时绝不宣称最优——即使恰好找到最优值，只要搜索未闭合，就如实
   报告 ``"timeout"`` 与当前界；唯一例外是超时后用剩余开放节点的界
   仍能证明 incumbent 最优（max open bound <= incumbent），此时升级
   为 ``"optimal"``（界说了算，而非间隙碰巧为 0）。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from functools import cmp_to_key

import numpy as np

from .bounds import fractional_bound
from .validation import GAP_EPS

# 每隔多少个“展开节点”检查一次时间（根节点额外检查一次）。
DEADLINE_CHECK_EVERY = 256


@dataclass
class SolveResult:
    """求解结果。所有数值均为 Python 原生类型，可直接 JSON 序列化。"""

    status: str                          # "optimal" | "timeout"
    objective: int                       # 最优值(optimal)或当前最好可行值(timeout)
    selected: list[int]                  # 选中物品的**原始索引**（升序）
    total_weight: int
    upper_bound: int                     # floor 后的整数上界（可直接核对）
    upper_bound_float: float             # 上界的浮点展示（分数背包松弛值）
    upper_bound_numerator: int           # 上界精确有理表示的分子
    upper_bound_denominator: int         # 分母（>0）
    relative_gap: float                  # (UB - obj) / max(1, |UB|)，展示用
    nodes_explored: int
    elapsed_seconds: float
    forced_zero_weight: list[int] = field(default_factory=list)  # 0 重量且被取的物品
    excluded_overweight: list[int] = field(default_factory=list)  # w>C 被剔除的物品
    n_items: int = 0
    capacity: int = 0

    def to_dict(self) -> dict:
        return {
            "status": self.status,
            "objective": self.objective,
            "selected": self.selected,
            "total_weight": self.total_weight,
            "upper_bound": self.upper_bound,
            "upper_bound_float": self.upper_bound_float,
            "upper_bound_numerator": self.upper_bound_numerator,
            "upper_bound_denominator": self.upper_bound_denominator,
            "relative_gap": self.relative_gap,
            "nodes_explored": self.nodes_explored,
            "elapsed_seconds": self.elapsed_seconds,
            "forced_zero_weight": self.forced_zero_weight,
            "excluded_overweight": self.excluded_overweight,
            "n_items": self.n_items,
            "capacity": self.capacity,
        }


def _density_cmp(a: tuple[int, int, int], b: tuple[int, int, int]) -> int:
    """按 v/w 降序；同密度按原始索引升序。a/b = (value, weight, orig_index)。"""

    va, wa, ia = a
    vb, wb, ib = b
    lhs = va * wb
    rhs = vb * wa
    if lhs > rhs:
        return -1
    if lhs < rhs:
        return 1
    return -1 if ia < ib else (1 if ia > ib else 0)


def _initial_greedy(
    items: list[tuple[int, int, int]],
    capacity: int,
) -> tuple[int, int, int]:
    """密度序贪心 + 单件最优，返回 (value, weight, bitmask)。

    位掩码的第 k 位对应排序后列表的第 k 件物品。
    """

    # --- 密度贪心（向量化累积容量）---
    v = np.array([it[0] for it in items], dtype=np.int64)
    w = np.array([it[1] for it in items], dtype=np.int64)
    if items:
        cum = np.cumsum(w, dtype=np.int64)
        take = cum <= capacity
        # take 是前缀：找到最后一个可取位置，取齐前缀。
        if bool(take.all()):
            k = len(items)
        else:
            k = int(np.argmax(~take))  # 第一件放不下的位置
        g_val = int(v[:k].sum()) if k > 0 else 0
        g_w = int(w[:k].sum()) if k > 0 else 0
        g_mask = (1 << k) - 1
    else:
        g_val = g_w = 0
        g_mask = 0

    # --- 单件最优：防止密度序把早期大件塞满、错过高价值单品 ---
    best_single = -1
    best_single_val = 0
    for k, (val, wt, _idx) in enumerate(items):
        if wt <= capacity and val > best_single_val:
            best_single_val = val
            best_single = k

    if best_single_val > g_val:
        return best_single_val, items[best_single][1], 1 << best_single
    return g_val, g_w, g_mask


def solve_knapsack(
    capacity: int,
    weights: list[int],
    values: list[int],
    timeout_seconds: float = 5.0,
) -> SolveResult:
    """求解 0/1 背包。调用前通常已由 :func:`knapsack.validation.validate_payload` 校验。"""

    n = len(weights)
    t_start = time.perf_counter()
    deadline = t_start + timeout_seconds

    # ---------------- 预处理：零重量物品 ----------------
    forced: list[int] = []
    forced_value = 0
    for i in range(n):
        if weights[i] == 0:
            if values[i] >= 0:
                forced.append(i)
                forced_value += values[i]
            # 负价值的零重量物品直接舍
    forced_set = set(forced)

    # ---------------- 预处理：超重物品剔除 ----------------
    excluded: list[int] = []
    candidates: list[tuple[int, int, int]] = []  # (value, weight, orig_index)
    for i in range(n):
        if i in forced_set:
            continue
        w = weights[i]
        v = values[i]
        if w > capacity:
            excluded.append(i)
            continue
        if v <= 0:
            # 非零重量且价值 <= 0：任何最优解都不会取（取了只会挤占容量
            # 或降低目标值），直接舍，缩小搜索规模。
            continue
        candidates.append((v, w, i))
    excluded.sort()

    # ---------------- 密度降序（精确比较）----------------
    items = sorted(candidates, key=cmp_to_key(_density_cmp))
    m = len(items)
    s_v = [it[0] for it in items]
    s_w = [it[1] for it in items]
    s_idx = [it[2] for it in items]

    # ---------------- 初始可行解 ----------------
    best_val, _greedy_w, best_mask = _initial_greedy(items, capacity)
    best_val += forced_value

    # 根上界（零重量物品的非负价值始终可拿，整数上界需另行加上）。
    # 节点栈中存储的上界**不含** forced_value：它表示“当前路径价值 cur
    # + 后缀分数松弛”，剪枝时再统一加 forced_value 与 incumbent 比较。
    suf_floor, _suf_num, _suf_den, _root_float = fractional_bound(
        s_v, s_w, 0, capacity,
    )
    nodes = 0

    def check_time() -> bool:
        return time.perf_counter() >= deadline

    # 栈元素：(depth, remaining_capacity, current_value, mask, bound_floor)。
    # bound_floor = current_value + 后缀分数松弛的整数 floor（不含
    # forced_value），在入栈时计算一次：一是剪枝判据，二是超时后直接作为
    # 该分支未探索部分的有效上界，无需重算或重放路径。
    stack: list[tuple[int, int, int, int, int]] = []
    stack.append((0, capacity, 0, 0, suf_floor))

    timed_out = False
    # 超时发生时**正在处理**的节点：它出栈即被中断，其子树尚未探索，
    # 但其上界在入栈前已算好——它是未探索空间的一部分，必须计入最终上界，
    # 否则“根节点即超时、栈为空”会被错误判定为搜索闭合。
    interrupted_bound: int | None = None

    while stack:
        depth, rem, cur, mask, b_floor = stack.pop()
        nodes += 1
        if nodes == 1 or (nodes & (DEADLINE_CHECK_EVERY - 1)) == 0:
            if check_time():
                timed_out = True
                interrupted_bound = b_floor
                break

        # 剪枝：该分支能达到的最高价值（含 forced_value）不超过 incumbent。
        if b_floor + forced_value <= best_val:
            continue

        if depth == m:
            cand = cur + forced_value
            if cand > best_val:
                best_val, best_mask = cand, mask
            continue

        v = s_v[depth]
        w = s_w[depth]

        # ---- “舍”分支：cur 不变，后缀上界 ----
        skip_suffix_floor, _sn, _sd, _ = fractional_bound(
            s_v, s_w, depth + 1, rem,
        )
        stack.append((depth + 1, rem, cur, mask, cur + skip_suffix_floor))

        # ---- “取”分支：容量允许时压栈（先探索）----
        if w <= rem:
            take_suffix_floor, _tn, _td, _ = fractional_bound(
                s_v, s_w, depth + 1, rem - w,
            )
            child_val = cur + v  # 当前物品整件计入，再叠加后缀上界
            stack.append((
                depth + 1, rem - w, child_val, mask | (1 << depth),
                child_val + take_suffix_floor,
            ))

    elapsed = time.perf_counter() - t_start

    # ---------------- 结果状态与最终上界 ----------------
    # 注意 best_w 只在 incumbent 更新时维护；为稳妥起见最后按掩码重算。
    chosen_sorted_positions = [k for k in range(m) if (best_mask >> k) & 1]
    selected = sorted(forced + [s_idx[k] for k in chosen_sorted_positions])
    total_weight = sum(weights[i] for i in selected)
    total_value = sum(values[i] for i in selected)
    # 掩码价值与累加价值必须一致（内部一致性检查，防实现错误被静默吞掉）。
    assert total_value == best_val, (total_value, best_val)

    if timed_out:
        # 剩余未探索解空间的上界 = 栈中所有开放节点上界的最大值；
        # 这些节点的上界在入栈时已精确算出（不含 forced_value），无需重算。
        open_floors = [b for (_d, _r, _c, _m, b) in stack]
        if interrupted_bound is not None:
            open_floors.append(interrupted_bound)
        # 已找到的可行解本身也是剩余空间的合法上界下界。
        suffix_incumbent = best_val - forced_value
        rem_ub = max(open_floors + [suffix_incumbent])
        final_ub = rem_ub + forced_value
        # 界说了算：即便没搜完，若剩余分支的上界已不能超过 incumbent，
        # 则最优性已被证明；否则如实报告 timeout，绝不把未闭合间隙当最优。
        status = "optimal" if final_ub <= best_val else "timeout"
        # 超时路径上各开放节点分母不同，不做有理合并；输出整数上界
        # （剪枝使用的就是它）供核对，有理字段给同值整数表示。
        ub_num, ub_den = final_ub, 1
        ub_float = float(final_ub)
    else:
        # 搜索完整闭合：最优值本身即上界。
        status = "optimal"
        final_ub = best_val
        ub_num, ub_den = best_val, 1
        ub_float = float(best_val)

    gap_den = max(1, abs(final_ub))
    raw_gap = (final_ub - best_val) / gap_den
    relative_gap = 0.0 if 0.0 <= raw_gap < GAP_EPS else raw_gap

    return SolveResult(
        status=status,
        objective=best_val,
        selected=selected,
        total_weight=total_weight,
        upper_bound=final_ub,
        upper_bound_float=ub_float,
        upper_bound_numerator=ub_num,
        upper_bound_denominator=ub_den,
        relative_gap=relative_gap,
        nodes_explored=nodes,
        elapsed_seconds=elapsed,
        forced_zero_weight=forced,
        excluded_overweight=excluded,
        n_items=n,
        capacity=capacity,
    )
