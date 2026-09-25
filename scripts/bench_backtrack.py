#!/usr/bin/env python3
"""灾难性回归基准：Thompson NFA vs 朴素回溯参考解释器。

用法:
    python scripts/bench_backtrack.py
对经典模式 (a+)+b 与全 'a' 输入（必然匹配失败）：
  * 参考解释器统计递归步数（带预算，超预算即终止）；
  * Thompson 引擎统计 NFA 模拟的边检查次数与墙钟时间。
输出 JSON，直观对比"指数 vs 线性"。
"""

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from renfa import Regex, reference
from renfa.reference import BudgetExhausted

BUDGET = 10_000_000
PATTERN = "(a+)+b"


def measure_reference(n: int) -> dict:
    try:
        steps = reference.steps_used_fullmatch(PATTERN, "a" * n, budget=BUDGET)
        return {"n": n, "steps": steps, "budget_exhausted": False}
    except BudgetExhausted as e:
        return {"n": n, "steps": None, "budget_exhausted": True,
                "budget": BUDGET, "note": str(e)}


def measure_thompson(n: int) -> dict:
    regex = Regex(PATTERN)
    t0 = time.perf_counter()
    result = regex.fullmatch("a" * n)
    elapsed = time.perf_counter() - t0
    return {"n": n, "edge_checks": regex.last_steps,
            "seconds": round(elapsed, 6), "matched": bool(result)}


def main() -> int:
    report = {"pattern": PATTERN, "input": "a" + "*n (无 b，必然失败)",
              "reference_budget": BUDGET, "results": []}
    ns_small = [8, 10, 12, 14, 16, 18, 20]
    ns_engine = [8, 16, 32, 64, 128, 256, 1_000, 10_000, 100_000]
    for n in ns_small:
        row = {"n": n, "reference": measure_reference(n)}
        if n in ns_engine:
            row["thompson"] = measure_thompson(n)
        report["results"].append(row)
    for n in ns_engine:
        if n > ns_small[-1]:
            report["results"].append(
                {"n": n, "thompson": measure_thompson(n)})

    # 线性性自检：n 加倍，边检查次数比值应 <= 3（留足实现常数余量）
    eng = {r["n"]: r["thompson"]["edge_checks"]
           for r in report["results"] if "thompson" in r}
    ratios = []
    for a, b in [(16, 32), (32, 64), (64, 128), (128, 256)]:
        if a in eng and b in eng:
            ratios.append(round(eng[b] / eng[a], 3))
    report["linearity_ratios_edgechecks_2x_n"] = ratios

    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
