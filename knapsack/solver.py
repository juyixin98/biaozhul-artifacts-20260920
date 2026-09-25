"""分支定界 0/1 背包求解器。

数值策略（容差说明）：
- 重量、价值、容量均为整数，所有可行性判断与最优性比较都在整数域进行，
  不涉及浮点容差。
- 上界使用线性规划松弛（分数背包），以 ``fractions.Fraction`` 精确有理数
  计算，不做任何浮点近似；整数上界取 floor(松弛值)。由于最优值必为整数，
  floor 后的上界仍然有效（>= 真实最优值）。
- 剪枝条件为 ``bound <= best``（精确比较）：整数目标下，节点只有当上界
  严格大于当前最优时才可能改进解。
- 因此“未闭合间隙”定义明确：``upper_bound - value > 0`` 时绝不报告
  optimal，只报告 feasible。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from fractions import Fraction
from typing import List, Optional, Tuple

import numpy as np

STATUS_OPTIMAL = "optimal"
STATUS_FEASIBLE = "feasible"  # 间隙未闭合（超时或达到节点上限）时的可行解状态

# 超时/节点数检查的频率（每处理多少个节点检查一次，避免 clock 调用开销）
_CHECK_INTERVAL = 512


@dataclass
class SolveResult:
    """求解结果。

    status: "optimal"（间隙闭合，已证明最优）或 "feasible"（未闭合，返回当前可行解）。
    value: 当前最好可行解的总价值（下界）。
    upper_bound: 可验证的全局整数上界，恒满足 upper_bound >= 真实最优值 >= value。
    selected: 被选中物品在输入数组中的下标。
    """

    status: str
    value: int
    upper_bound: int
    selected: List[int]
    nodes: int
    elapsed_ms: float
    reason: str = ""

    @property
    def gap(self) -> int:
        return self.upper_bound - self.value


class _SortedInstance:
    """按密度（价值/重量）降序排列后的实例及前缀和，用于 O(log n) 上界计算。"""

    def __init__(self, weights: np.ndarray, values: np.ndarray, indices: List[int]):
        self.m = len(indices)
        self.sw = weights  # 已排序重量（int64），均 >= 1
        self.sv = values  # 已排序价值（int64），均 >= 1
        self.indices = indices  # 排序后位置 -> 原始下标
        # 前缀和：pw[j] = 前 j 个物品重量和，pv 同理；长度 m+1，pw[0]=0
        self.pw = np.zeros(self.m + 1, dtype=np.int64)
        self.pv = np.zeros(self.m + 1, dtype=np.int64)
        np.cumsum(self.sw, out=self.pw[1:])
        np.cumsum(self.sv, out=self.pv[1:])

    def lp_bound(self, i: int, cap: int, base_value: int) -> Fraction:
        """从第 i 个（排序后）物品起、剩余容量 cap 的分数背包松弛上界（精确有理数）。

        base_value 为已固定选择部分的价值。返回值 >= 该子树内任意整数解的价值。
        """
        m = self.m
        if i >= m or cap <= 0:
            return Fraction(base_value)
        limit = int(self.pw[i]) + cap
        # j = 满足 pw[j] <= limit 的最大 j（即物品 i..j-1 可完整装入）
        j = int(np.searchsorted(self.pw, limit, side="right"))
        if j > m:
            j = m
        value = base_value + int(self.pv[j]) - int(self.pv[i])
        if j < m:
            used = int(self.pw[j]) - int(self.pw[i])
            rem = cap - used  # 0 <= rem < sw[j]
            if rem > 0:
                value_frac = Fraction(int(self.sv[j]) * rem, int(self.sw[j]))
                return Fraction(value) + value_frac
        return Fraction(value)


def _preprocess(
    weights: np.ndarray, values: np.ndarray
) -> Tuple[List[int], int, List[int]]:
    """分类物品，返回 (kept 原始下标, 基础价值, 必取物品原始下标)。

    - 零重量且正价值：必取（不消耗容量），计入基础价值。
    - 零重量且非正价值：永不取。
    - 正重量且非正价值：永不取（只消耗容量、不增加价值；空解恒可行）。
    - 其余（正重量正价值）进入分支定界。
    """
    kept: List[int] = []
    auto: List[int] = []
    base = 0
    for i in range(len(weights)):
        w = int(weights[i])
        v = int(values[i])
        if w == 0:
            if v > 0:
                base += v
                auto.append(i)
            # v <= 0 且 w == 0：不取
        elif v <= 0:
            pass  # 不取
        else:
            kept.append(i)
    return kept, base, auto


def solve(
    weights,
    values,
    capacity: int,
    time_limit_sec: Optional[float] = None,
    max_nodes: Optional[int] = None,
) -> SolveResult:
    """求解 0/1 背包：max sum(v_i x_i) s.t. sum(w_i x_i) <= capacity, x in {0,1}。

    参数均为整数（可传入列表或 NumPy 数组）。调用方需保证输入已通过校验
    （见 knapsack.validate / knapsack.api）。

    返回 SolveResult；仅当分支定界树完全展开（间隙闭合为 0）时 status 为
    "optimal"，否则为 "feasible" 并附带仍然有效的全局上界。
    """
    t0 = time.perf_counter()
    w = np.asarray(weights, dtype=np.int64).ravel()
    v = np.asarray(values, dtype=np.int64).ravel()
    capacity = int(capacity)

    kept, base_value, auto = _preprocess(w, v)

    def finish(status, best, ub, chosen, nodes, reason=""):
        elapsed = (time.perf_counter() - t0) * 1000.0
        return SolveResult(
            status=status,
            value=int(best),
            upper_bound=int(ub),
            selected=sorted(auto + list(chosen)),
            nodes=nodes,
            elapsed_ms=elapsed,
            reason=reason,
        )

    if not kept:
        # 无可分支物品：必取项即为最优
        return finish(STATUS_OPTIMAL, base_value, base_value, [], 0)

    kw = w[kept]
    kv = v[kept]

    # 全部装得下：直接取完，即最优
    if int(kw.sum()) <= capacity:
        total = base_value + int(kv.sum())
        return finish(STATUS_OPTIMAL, total, total, kept, 0)

    # 按密度降序排序（等密度物品顺序任意，不影响正确性；用稳定排序保证可复现）
    order = sorted(range(len(kept)), key=lambda k: Fraction(int(kv[k]), int(kw[k])), reverse=True)
    sw = kw[order]
    sv = kv[order]
    sorted_indices = [kept[k] for k in order]
    inst = _SortedInstance(sw, sv, sorted_indices)
    m = inst.m

    best_value = 0  # 相对于 base_value 的增量最优
    best_chosen: Tuple[int, ...] = ()

    # 显式栈的 DFS。栈元素：(子树上界, 起始物品, 已选增量价值, 剩余容量, 已选原始下标)
    root_bound = inst.lp_bound(0, capacity, 0)
    stack: List[Tuple[Fraction, int, int, int, Tuple[int, ...]]] = [
        (root_bound, 0, 0, capacity, ())
    ]
    nodes = 0
    reason = ""

    deadline = None if time_limit_sec is None else t0 + time_limit_sec

    while stack:
        # 节点上限每次迭代都检查（整数比较开销可忽略）；时钟检查按间隔进行
        if max_nodes is not None and nodes >= max_nodes:
            reason = f"node limit reached ({max_nodes})"
            break
        if deadline is not None and nodes % _CHECK_INTERVAL == 0:
            if time.perf_counter() >= deadline:
                reason = "time limit reached"
                break

        bound, i, val, cap, chosen = stack.pop()
        nodes += 1

        # 精确剪枝：整数目标下 bound <= best 即无法改进
        if bound <= best_value:
            continue

        if i >= m:
            # 叶子：全部物品已决策
            if val > best_value:
                best_value = val
                best_chosen = chosen
            continue

        item_idx = sorted_indices[i]
        wi = int(sw[i])
        vi = int(sv[i])

        # 先压入“不取”分支，后压入“取”分支（栈为 LIFO，优先探索“取”）
        skip_bound = inst.lp_bound(i + 1, cap, val)
        if skip_bound > best_value:
            stack.append((skip_bound, i + 1, val, cap, chosen))

        if wi <= cap:
            take_val = val + vi
            take_cap = cap - wi
            take_bound = inst.lp_bound(i + 1, take_cap, take_val)
            if take_bound > best_value:
                stack.append((take_bound, i + 1, take_val, take_cap, chosen + (item_idx,)))
            elif take_val > best_value and i + 1 >= m:
                # 叶子子节点被剪枝前仍可能更新最优
                best_value = take_val
                best_chosen = chosen + (item_idx,)

    if stack:
        # 提前终止：全局上界 = max(当前最优, 栈中所有未探索节点的上界)
        global_ub_frac = max([Fraction(best_value)] + [entry[0] for entry in stack])
        # floor 对整数 best_value 无副作用；对分数上界取 floor 仍有效（最优值为整数）
        global_ub = global_ub_frac.numerator // global_ub_frac.denominator
        global_ub = max(global_ub, best_value)
        return finish(
            STATUS_FEASIBLE,
            base_value + best_value,
            base_value + global_ub,
            list(best_chosen),
            nodes,
            reason=reason or "terminated with open gap",
        )

    # 栈空：所有节点已展开或被证明无法改进，间隙闭合
    total = base_value + best_value
    return finish(STATUS_OPTIMAL, total, total, list(best_chosen), nodes)
