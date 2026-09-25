#!/usr/bin/env python3
"""Beale 循环问题演示：Bland vs Dantzig，并与顶点枚举参考解对照。

用法：python3 scripts/beale_demo.py
"""

import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bounded_lp import LPProblem, solve  # noqa: E402
from bounded_lp.enumerate_vertices import reference_solve  # noqa: E402


def main() -> int:
    c = [-3 / 4, 150, -1 / 50, 6, 0, 0, 0]
    A = [
        [1 / 4, -60, -1 / 25, 9, 1, 0, 0],
        [1 / 2, -90, -1 / 50, 3, 0, 1, 0],
        [0, 0, 1, 0, 0, 0, 1],
    ]
    p = LPProblem(c=c, A_eq=A, b_eq=[0, 0, 1])

    rb = solve(p, rule="bland")
    rd = solve(p, rule="dantzig", max_iterations=500)
    st, info = reference_solve(p)

    print(f"Bland   : {rb.status:9s} obj={rb.objective:.4f} "
          f"p1={rb.phase1_iterations} p2={rb.phase2_iterations}")
    tail = f"reason={rd.reason}" if rd.reason else f"obj={rd.objective:.4f}"
    print(f"Dantzig : {rd.status:9s} {tail} "
          f"p1={rd.phase1_iterations} p2={rd.phase2_iterations}  (浮点)")
    print(f"枚举参考 : {st:9s} obj={info['objective']:.4f}")
    print()
    print("提示：浮点 Dantzig 可能被舍入扰动“解救”；精确算术下的确定性循环见")
    print("      tests/test_simplex_core.py::test_beale_cycles_under_exact_arithmetic")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
