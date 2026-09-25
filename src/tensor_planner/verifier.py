"""规划正确性验证：活跃张量不重叠、峰值预算、与基线执行器结果一致。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Mapping, Tuple

import numpy as np

from .dag import DAG
from .executor import (
    ITEMSIZE,
    ExecutionResult,
    NaiveExecutor,
    ReuseExecutor,
    max_abs_diff,
)
from .planner import Plan, build_plan


@dataclass
class OverlapViolation:
    """两个同时存活的存储组落在同一 arena 槽位的冲突。"""

    root_a: str
    root_b: str
    offset: int
    time: int
    interval_a: Tuple[int, int]
    interval_b: Tuple[int, int]


@dataclass
class Verification:
    """完整验证结果。"""

    passed: bool
    plan: Plan
    # 1) 不重叠
    overlap_violations: List[OverlapViolation] = field(default_factory=list)
    # 2) 槽位不越界
    out_of_bounds: List[str] = field(default_factory=list)
    # 3) 视图别名落在基底槽位内部
    alias_errors: List[str] = field(default_factory=list)
    # 4) 峰值预算（作用于复用执行器的物理 arena）
    arena_elements: int = 0
    arena_bytes: int = 0
    peak_live_elements: int = 0
    within_budget: bool = True
    budget_elements: int | None = None
    # 5) 与基线对比
    # 峰值：两种执行模型口径不同（基线对别名逐份计数，复用按物理槽计数），
    # 如实并列，不假设大小关系；累计分配量是无歧义的复用收益指标。
    naive_peak_elements: int = 0
    reuse_peak_elements: int = 0
    naive_total_allocated: int = 0
    reuse_total_allocated: int = 0
    saved_total_elements: int = 0
    saved_total_ratio: float = 0.0
    max_abs_diff: float = 0.0
    output_names: List[str] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)

    def summary(self) -> str:
        lines = [
            f"passed={self.passed}",
            f"arena: {self.arena_elements} 元素 / {self.arena_bytes} 字节",
            f"复用峰值存活: {self.peak_live_elements} 元素"
            + (
                f" (预算 {self.budget_elements} 内)"
                if self.budget_elements is not None
                else ""
            ),
            f"峰值存活(元素) 基线={self.naive_peak_elements} "
            f"复用={self.reuse_peak_elements}",
            f"累计分配(元素) 基线={self.naive_total_allocated} "
            f"复用={self.reuse_total_allocated} "
            f"节省 {self.saved_total_elements} ({self.saved_total_ratio:.1%})",
            f"与不复用执行器最大绝对误差: {self.max_abs_diff:.3e}",
        ]
        if self.overlap_violations:
            lines.append(f"重叠冲突: {len(self.overlap_violations)} 个")
        if self.errors:
            lines.append("错误: " + "; ".join(self.errors))
        return "\n".join(lines)


def check_no_overlap(plan: Plan) -> List[OverlapViolation]:
    """断言：任意时刻，同时存活的存储组所占 arena 字节区间互不相交。

    端点相接（一个在 t 读完死亡、另一个在 t 出生）允许同址，不算冲突。
    """
    violations: List[OverlapViolation] = []
    slots = list(plan.placements.values())
    for i, a in enumerate(slots):
        for b in slots[i + 1 :]:
            # 时间严格重叠？
            if not a.interval.overlaps(b.interval):
                continue
            # 地址区间相交（半开，端点相接允许）？
            if a.offset < b.offset + b.size and b.offset < a.offset + a.size:
                t = max(a.interval.start, b.interval.start)
                violations.append(
                    OverlapViolation(
                        root_a=a.root,
                        root_b=b.root,
                        offset=max(a.offset, b.offset),
                        time=t,
                        interval_a=(a.interval.start, a.interval.end),
                        interval_b=(b.interval.start, b.interval.end),
                    )
                )
    return violations


def check_bounds(plan: Plan) -> List[str]:
    """所有槽位必须位于 arena 内部。"""
    errors: List[str] = []
    for root, slot in plan.placements.items():
        if slot.offset < 0 or slot.offset + slot.size > plan.arena_size:
            errors.append(
                f"存储组 {root!r} 槽位 [{slot.offset}, "
                f"{slot.offset + slot.size}) 越出 arena(0, {plan.arena_size})"
            )
    return errors


def check_views_alias_within_base(dag: DAG, plan: Plan) -> List[str]:
    """切片视图必须解析到基底槽位内部（共享存储的必要条件）。"""
    errors: List[str] = []
    for base, view_ops in dag.slices.items():
        base_slot = plan.placements[base]
        base_len = dag.num_elements(base)
        for op in view_ops:
            offset = dag.view_offset(op.name)
            size = dag.num_elements(op.name)
            if not (0 <= offset and offset + size <= base_len):
                errors.append(
                    f"视图 {op.name!r} 扁平区间 [{offset}, {offset + size}) "
                    f"超出基底 {base!r}（{base_len} 元素）"
                )
            # 视图通过基底的放置访问，二者必须是同一存储组
            if plan.root_of_value.get(op.name) != base:
                errors.append(
                    f"视图 {op.name!r} 未登记到基底 {base!r} 的存储组"
                )
            # 偏移不得使视图逻辑地址越过该槽位
            if base_slot.offset + offset + size > base_slot.end:
                errors.append(
                    f"视图 {op.name!r} 在 arena 中越出基底槽位"
                )
    return errors


def outputs_max_abs_diff(
    dag: DAG, feeds: Mapping[str, np.ndarray]
) -> Tuple[Dict[str, np.ndarray], Dict[str, np.ndarray], float]:
    """运行两个执行器并返回 (基线输出, 复用输出, 最大绝对误差)。"""
    naive = NaiveExecutor(dag).run(feeds)
    reuse = ReuseExecutor(dag, build_plan(dag)).run(feeds)
    diff = max_abs_diff(naive.outputs, reuse.outputs)
    return naive.outputs, reuse.outputs, diff


def _structural_errors(
    dag: DAG, plan: Plan
) -> Tuple[List[OverlapViolation], List[str], List[str], List[str]]:
    """静态结构检查：不重叠、不越界、别名合法。返回 (冲突, 越界, 别名错, 错误)。"""
    overlaps = check_no_overlap(plan)
    bounds = check_bounds(plan)
    alias = check_views_alias_within_base(dag, plan)
    errors = [f"重叠 {v.root_a!r}/{v.root_b!r} @t={v.time}" for v in overlaps]
    errors.extend(bounds)
    errors.extend(alias)
    return overlaps, bounds, alias, errors


def _run_and_compare(
    dag: DAG, plan: Plan, feeds: Mapping[str, np.ndarray]
) -> Tuple[float, List[str], ExecutionResult, ExecutionResult]:
    """运行双执行器并比较输出。返回 (最大误差, 错误, 基线结果, 复用结果)。"""
    naive_result = NaiveExecutor(dag).run(feeds)
    reuse_result = ReuseExecutor(dag, plan).run(feeds)
    errors: List[str] = []
    try:
        diff = max_abs_diff(naive_result.outputs, reuse_result.outputs)
    except ValueError as exc:
        diff = float("inf")
        errors.append(str(exc))
    if not np.isfinite(diff) or diff > 1e-12:
        errors.append(f"输出与基线不一致: max_abs_diff={diff}")
    return diff, errors, naive_result, reuse_result


def _consistency_errors(
    plan: Plan,
    reuse_peak: int,
    budget_elements: int | None,
) -> Tuple[bool, List[str]]:
    """预算与规划自洽性检查。返回 (是否在预算内, 错误)。"""
    errors: List[str] = []
    within = True
    if budget_elements is not None:
        within = plan.arena_size <= budget_elements
        if not within:
            errors.append(
                f"arena {plan.arena_size} 超出峰值预算 {budget_elements}"
            )
    # 执行器实测峰值必须与规划静态峰值一致
    if reuse_peak != plan.peak_live_elements:
        errors.append(
            f"复用执行器实测峰值 {reuse_peak} 与"
            f" 规划静态峰值 {plan.peak_live_elements} 不一致"
        )
    # 复用执行器的物理峰值不得超过 arena（预算保证的核心不变量）
    if plan.peak_live_elements > plan.arena_size:
        errors.append(
            f"物理峰值 {plan.peak_live_elements} 超过 arena "
            f"{plan.arena_size}（规划自相矛盾）"
        )
    return within, errors


def verify_plan(
    dag: DAG,
    feeds: Mapping[str, np.ndarray],
    budget_elements: int | None = None,
) -> Verification:
    """端到端验证一个 DAG 的内存规划。

    检查项：
    1. 同时存活的存储组 arena 区间互不重叠；
    2. 槽位不越界；
    3. 切片视图正确别名到基底槽位；
    4. 峰值占用不超过给定预算（若提供）；
    5. 复用执行器输出与不复用基线逐元素一致。
    """
    plan = build_plan(dag)
    overlaps, bounds, alias, errors = _structural_errors(dag, plan)
    diff, run_errors, naive_result, reuse_result = _run_and_compare(
        dag, plan, feeds
    )
    errors.extend(run_errors)
    within, consistency = _consistency_errors(
        plan, reuse_result.peak_elements, budget_elements
    )
    errors.extend(consistency)

    naive_total = naive_result.total_allocated_elements
    reuse_total = reuse_result.total_allocated_elements
    saved_total = naive_total - reuse_total

    return Verification(
        passed=not errors,
        plan=plan,
        overlap_violations=overlaps,
        out_of_bounds=bounds,
        alias_errors=alias,
        arena_elements=plan.arena_size,
        arena_bytes=plan.arena_size * ITEMSIZE,
        peak_live_elements=plan.peak_live_elements,
        within_budget=within,
        budget_elements=budget_elements,
        naive_peak_elements=naive_result.peak_elements,
        reuse_peak_elements=reuse_result.peak_elements,
        naive_total_allocated=naive_total,
        reuse_total_allocated=reuse_total,
        saved_total_elements=saved_total,
        saved_total_ratio=saved_total / naive_total if naive_total else 0.0,
        max_abs_diff=diff,
        output_names=sorted(naive_result.outputs),
        errors=errors,
    )
