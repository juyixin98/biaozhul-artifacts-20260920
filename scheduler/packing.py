"""
GPU 装箱（bin packing）。

输入：某作业的 GPU 需求（min/max、单卡显存）与候选节点的空闲容量快照；
输出：{node_id: 分配卡数} 的分配计划，或判定资源不足/碎片化。

规则（需求要求显式说明）：
1. **资格过滤**：只考虑可调度（available/occupied）节点，且节点单卡
   显存 >= 作业要求。drain / offline 节点绝不进入候选。
2. **尽力分配**：目标满足 max_gpus；受容量限制时只要总量 >= min_gpus
   也算可分配（作业声明了弹性区间）。
3. **装箱策略 —— Best-Fit Decreasing（最佳适应降序）**：
   - 节点先按空闲卡数**升序**排序（“最小能装下的节点优先”），尽量把
     作业塞进紧凑的节点，保留大块连续空间给需要整节点的大作业，降低
     碎片；
   - 单卡显存更大的节点留到后面（同空闲量时优先用显存刚好够的节点，
     把大显存节点留给高显存需求作业）；
   - 再按节点 id 稳定排序，保证结果可复现。
   - 每次取当前第一个“装得下至少 1 张”的节点（即 best fit），尽量取
     该节点全部空闲卡，直到凑到 max_gpus。
4. **碎片化判定**：若总空闲容量 >= min_gpus 但没有任何节点能放下
   （单节点都装不下 1 张且无法拆分，此处作业可跨节点拆分，因此仅当
   总量不足 min 才失败），记录碎片原因。本实现允许跨节点拆分，所以
   “容量够但分不出来”的唯一情形是所有候选单卡显存都不达标或总量不足；
   仍会在理由中保留候选快照以便排查碎片。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class NodeCapacity:
    node_id: int
    free_gpus: int
    total_gpus: int
    gpu_memory_mb: int
    status: str


def _sort_candidates(candidates: list[NodeCapacity]) -> list[NodeCapacity]:
    # 空闲升序（best-fit），显存升序（够用即可，留大显存给大需求），
    # 最后 node_id 升序保证确定性。
    return sorted(
        candidates,
        key=lambda c: (c.free_gpus, c.gpu_memory_mb, c.node_id),
    )


def plan_allocation(
    *,
    min_gpus: int,
    max_gpus: int,
    required_memory_mb: int,
    candidates: list[NodeCapacity],
) -> dict:
    """
    返回 {
        "feasible": bool,
        "plan": {node_id: gpu_count},
        "allocated_gpus": int,
        "reason": str,
        "snapshot": [...],   # 排序后候选，供审计
    }
    """
    eligible = [
        c
        for c in candidates
        if c.free_gpus > 0 and c.gpu_memory_mb >= required_memory_mb
    ]
    ordered = _sort_candidates(eligible)

    total_free = sum(c.free_gpus for c in ordered)
    ceiling = min(max_gpus, total_free)

    plan: dict[int, int] = {}
    remaining = ceiling
    for cand in ordered:
        if remaining <= 0:
            break
        take = min(cand.free_gpus, remaining)
        if take > 0:
            plan[cand.node_id] = take
            remaining -= take

    allocated = ceiling - remaining

    if allocated >= min_gpus:
        return {
            "feasible": True,
            "plan": plan,
            "allocated_gpus": allocated,
            "reason": (
                f"best-fit 装箱满足 {allocated} 张 "
                f"(需求 {min_gpus}-{max_gpus}, 显存>={required_memory_mb}MB)"
            ),
            "snapshot": [c.__dict__ for c in ordered],
        }

    # 不可行：区分“显存不足 / 总量不足 / 碎片”。
    if not ordered:
        if candidates:
            reason = (
                f"无满足单卡显存>={required_memory_mb}MB 的可调度节点"
                "（可能是排空/离线或显存不足）"
            )
        else:
            reason = "没有可调度节点（全部排空/离线）"
    elif total_free < min_gpus:
        reason = (
            f"在线可调度空闲 GPU 共 {total_free} 张 < 最少需求 {min_gpus} 张"
        )
    else:
        reason = (
            f"资源碎片化：空闲 {total_free} 张但无法凑出 {min_gpus} 张"
            "（显存或节点容量限制）"
        )

    return {
        "feasible": False,
        "plan": {},
        "allocated_gpus": allocated,
        "reason": reason,
        "snapshot": [c.__dict__ for c in ordered],
    }


def job_sort_key(job, preemptor_priority: Optional[int] = None):
    """
    排队作业的调度顺序键（同优先级排序规则，需求要求显式说明）。

    - **优先级降序**：priority 高的先调度（10 最先）。
    - **同优先级按入队时间升序**：FIFO，先到先得。
    - 再以 job id 升序兜底，保证完全确定、可复现。

    这意味着高优先级后到的作业会排在低优先级前面；是否允许抢占由
    调度器按 8/3 阈值单独判断，与本排序解耦。
    """
    return (-int(job.priority), job.queued_at, job.id)
