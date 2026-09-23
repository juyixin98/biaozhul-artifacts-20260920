"""可复现的额外核验：下界合法性 vs 独立网格穷举（非 unittest 断言集）。

用法：``python3 tests/check_lower_bounds.py [case_count] [seed]``

对随机整数小例，用网格位图穷举求真实最优高度，断言本库的下界
从不超过它（下界一旦超过真实最优即为算法错误）。默认 300 例。
退出码 0 表示全部通过。
"""

import os
import random
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from strip_packing.lower_bounds import lower_bounds  # noqa: E402
from grid_oracle import grid_optimal_height  # noqa: E402


def main(count=300, seed=1):
    rng = random.Random(seed)
    violations = 0
    for t in range(count):
        W = rng.randint(3, 8)
        n = rng.randint(1, 6)
        pairs = [(rng.randint(1, W), rng.randint(1, 6)) for _ in range(n)]
        rects = [(i, float(a), float(b)) for i, (a, b) in enumerate(pairs)]
        lbs = lower_bounds(rects, W)
        optimum = grid_optimal_height(pairs, W, 60)
        for name in ("area", "max_height", "pairwise"):
            if lbs[name] > optimum + 1e-9:
                violations += 1
                print(f"VIOLATION case={t} W={W} pairs={pairs} "
                      f"LB[{name}]={lbs[name]} > optimum={optimum}")
    print(f"checked {count} random cases; lower-bound violations: {violations}")
    return 1 if violations else 0


if __name__ == "__main__":
    n_cases = int(sys.argv[1]) if len(sys.argv) > 1 else 300
    seed = int(sys.argv[2]) if len(sys.argv) > 2 else 1
    sys.exit(main(n_cases, seed))
