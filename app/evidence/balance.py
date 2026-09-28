"""均衡核算：枚举每个区组的实际计数与比例。

输出把**完整区组**与**未封闭/未排满尾组**严格分列：
- 完整区组：比例是硬约束，任何偏差都是 FAIL（机制被破坏）；
- 尾组：比例偏差是*结构性不确定*，登记未结束，既不算通过也不算失败。
"""
from __future__ import annotations

from collections import Counter
from typing import Any

from ..contracts import AllocationContract


def balance_report(contract: AllocationContract, blocks: list,
                   allocations: list) -> dict[str, Any]:
    expected = contract.arm_slots_per_block()
    arm_ids = contract.arm_ids
    block_reports: list[dict] = []
    failures: list[dict] = []
    uncertain: list[dict] = []

    alloc_by_block: dict[int, list] = {}
    for a in allocations:
        alloc_by_block.setdefault(a["block_id"], []).append(a)

    totals = Counter()
    completed_blocks = 0
    for b in blocks:
        members = alloc_by_block.get(b["id"], [])
        counts = Counter(m["arm"] for m in members)
        filled = len(members)
        size = int(b["block_size"])
        is_full = filled == size
        status = b["status"]
        entry: dict[str, Any] = {
            "stratum_key": b["stratum_key"],
            "block_index": int(b["block_index"]),
            "block_size": size,
            "filled": filled,
            "status": status,
            "counts": {arm: int(counts.get(arm, 0)) for arm in arm_ids},
        }
        if is_full:
            completed_blocks += 1
            exact = {arm: expected[arm] for arm in arm_ids}
            entry["expected_counts"] = exact
            deviations = {
                arm: int(counts.get(arm, 0)) - exact[arm] for arm in arm_ids
            }
            entry["deviations"] = deviations
            if any(v != 0 for v in deviations.values()):
                failures.append({
                    "category": "BLOCK_RATIO_VIOLATION",
                    "stratum_key": b["stratum_key"],
                    "block_index": int(b["block_index"]),
                    "observed": entry["counts"],
                    "expected": exact,
                })
            for arm in arm_ids:
                totals[arm] += int(counts.get(arm, 0))
        else:
            # 尾组：给出若按契约本应有的槽位与当前缺口，标记为不确定
            missing = size - filled
            entry["expected_counts_if_completed"] = expected
            entry["missing_to_completion"] = missing
            note = {
                "category": "INCOMPLETE_TAIL_BLOCK",
                "stratum_key": b["stratum_key"],
                "block_index": int(b["block_index"]),
                "filled": filled,
                "block_size": size,
                "status": status,
                "reason": (
                    "尾组未排满，比例天然不可能精确；这是登记未结束的"
                    "结构性不确定，不构成机制失败"
                    if status == "open"
                    else "尾组已提前封闭（seal_early），按设计不补齐"
                ),
            }
            uncertain.append(note)
            for arm in arm_ids:
                totals[arm] += int(counts.get(arm, 0))
        block_reports.append(entry)

    total_n = sum(totals.values())
    ratio_sum = contract.ratio_sum
    aggregate = {
        "n_allocated": total_n,
        "counts": {arm: int(totals.get(arm, 0)) for arm in arm_ids},
        "observed_proportions": {
            arm: (round(totals.get(arm, 0) / total_n, 6) if total_n else None)
            for arm in arm_ids
        },
        "target_proportions": {
            arm: contract.arms[i].ratio / ratio_sum
            for i, arm in enumerate(arm_ids)
        },
        "completed_blocks": completed_blocks,
    }
    overall = "PASS" if not failures else "FAIL"
    return {
        "study_id": contract.study_id,
        "contract_fingerprint": contract.fingerprint(),
        "verdict": overall,
        "blocks": block_reports,
        "aggregate": aggregate,
        "failures": failures,
        "uncertainties": uncertain,
        "interpretation": (
            "完整区组比例由槽位铺设确定性保证；尾组偏差单列，不计入失败。"
            "本报告不涉及任何分配后效果指标。"
        ),
    }
