"""上界有效性专项测试。

性质（对任意实例、任意时刻都必须成立）：
    objective <= upper_bound <= LP 松弛根上界 <= “按密度贪心拆分”手算值

这里独立用一套朴素的分数背包实现（浮点 + fractions 交叉核对）验证
求解器输出的整数上界确实夹住最优值，且不依赖被测代码自身的排序。
"""

from __future__ import annotations

import os
import random
import sys
from fractions import Fraction

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from knapsack.api import solve  # noqa: E402


def independent_root_lp(weights, values, capacity):
    """独立实现的根 LP 松弛值（精确 Fraction），忽略零重量物品之外直接给。"""

    total = Fraction(0)
    # 零重量、非负价值物品直接计入；负价值不取。
    rem = capacity
    items = []
    forced = 0
    for w, v in zip(weights, values):
        if w == 0:
            if v >= 0:
                forced += v
        else:
            items.append((w, v))
    items.sort(key=lambda t: -Fraction(t[1], t[0]))
    for w, v in items:
        if v <= 0:
            continue  # LP 最优解中负价值物品必为 0（下界 x_i >= 0）
        if w > rem:
            total += Fraction(v) * rem / w
            rem = 0
            break
        total += v
        rem -= w
    return forced + total


def test_bound_is_valid_random():
    rng = random.Random(7)
    for seed in range(300):
        n = rng.randint(0, 14)
        weights = [
            0 if rng.random() < 0.1 else rng.randint(1, 10)
            for _ in range(n)
        ]
        values = [rng.randint(-4, 15) for _ in range(n)]
        cap = rng.randint(0, 25)
        resp = solve({"capacity": cap, "weights": weights, "values": values})
        assert resp["ok"]
        obj = resp["objective"]
        ub = resp["upper_bound"]
        # 1) 上界夹住目标值
        assert ub >= obj, (seed, weights, values, cap, resp)
        # 2) 有理字段自洽：num/den 是给出的上界
        exact = Fraction(resp["upper_bound_numerator"], resp["upper_bound_denominator"])
        assert exact >= obj, (seed, resp)
        # 3) optimal 时三者必须相等；任何状态下 UB 不超过独立根 LP 上界
        lp = independent_root_lp(weights, values, cap)
        assert ub <= lp, (seed, weights, values, cap, resp, float(lp))
        assert exact <= lp, (seed, resp, float(lp))
        if resp["status"] == "optimal":
            assert ub == obj
            assert resp["relative_gap"] == 0.0
    print("test_bound_is_valid_random: 300 个实例上界全部有效（obj <= UB <= LP）")


def test_fractional_bound_unit():
    """bounds.fractional_bound 的小例手算核对。"""

    from knapsack.bounds import fractional_bound

    # 物品已按密度序：5/4 = 1.25 > 6/5 = 1.2。
    vals, ws = [5, 6], [4, 5]
    fl, num, den, fv = fractional_bound(vals, ws, 0, 6)
    # 整件拿 (v=5,w=4) 剩 2，再拆分拿 6 的 2/5 = 2.4 -> 7.4, floor 7
    assert (fl, num, den) == (7, 37, 5), (fl, num, den)
    assert abs(fv - 7.4) < 1e-12

    # 容量 0
    assert fractional_bound(vals, ws, 0, 0)[0] == 0
    # 容量足够装全部
    fl2, num2, den2, _ = fractional_bound(vals, ws, 0, 100)
    assert (fl2, num2, den2) == (11, 11, 1)
    print("test_fractional_bound_unit: 通过")


def test_gap_nonnegative_and_never_false_optimal():
    """relative_gap 恒非负；optimal 绝不在有间隙时出现。"""

    rng = random.Random(99)
    for _ in range(100):
        n = rng.randint(1, 12)
        weights = [rng.randint(1, 8) for _ in range(n)]
        values = [rng.randint(0, 10) for _ in range(n)]
        cap = rng.randint(0, 20)
        resp = solve({"capacity": cap, "weights": weights, "values": values})
        assert resp["relative_gap"] >= 0.0
        if resp["status"] == "optimal":
            assert resp["upper_bound"] == resp["objective"]
    print("test_gap_nonnegative_and_never_false_optimal: 通过")
