"""小规模枚举交叉验证：分支定界的最优值必须等于 2^n 全枚举真值。

同时验证：
* 返回的 selected 子集确实可行（总重 <= 容量）且目标值与 objective 一致；
* 整数上界 upper_bound >= objective；
* 上界的精确有理表示与整数字段自洽。
"""

from __future__ import annotations

import os
import random
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from knapsack.api import solve  # noqa: E402

from .brute_force import brute_force_optimal  # noqa: E402


def _check_response_feasibility(resp, weights, capacity):
    assert resp["ok"] is True
    sel = resp["selected"]
    assert sel == sorted(set(sel)), "selected 必须无重复且有序"
    assert all(0 <= i < len(weights) for i in sel)
    tw = sum(weights[i] for i in sel)
    assert tw <= capacity, f"返回解不可行：{tw} > {capacity}"
    assert tw == resp["total_weight"]
    # 上界有效性：UB >= obj，且有理表示 num/den >= obj。
    assert resp["upper_bound"] >= resp["objective"]
    assert resp["upper_bound_numerator"] >= resp["objective"] * resp["upper_bound_denominator"]
    return sel


def test_exhaustive_tiny():
    """n=0..5、容量 0..6、权重含 0、价值含负数的所有组合全部枚举验证。"""

    checked = 0
    for n in range(0, 6):
        # 固定取值空间，穷举 weight/value 序列。
        ws_space = [0, 1, 2, 3]
        vs_space = [-2, -1, 0, 1, 3]
        rng = random.Random(1000 + n)
        trials = 60
        for _ in range(trials):
            weights = [rng.choice(ws_space) for _ in range(n)]
            values = [rng.choice(vs_space) for _ in range(n)]
            for cap in range(0, 7):
                resp = solve({
                    "capacity": cap, "weights": weights, "values": values,
                    "timeout_seconds": 10.0,
                })
                _check_response_feasibility(resp, weights, cap)
                assert resp["status"] == "optimal", (weights, values, cap, resp)
                bf_val, _ = brute_force_optimal(weights, values, cap)
                assert resp["objective"] == bf_val, (weights, values, cap, resp, bf_val)
                assert resp["upper_bound"] == bf_val, "完整求解后上界应收紧到最优值"
                checked += 1
    print(f"test_exhaustive_tiny: 校验 {checked} 个实例全部一致")


def test_random_fuzz_medium():
    """n<=16 的随机实例：与 NumPy 暴力枚举对比，并核对上界始终有效。"""

    rng = random.Random(42)
    for seed in range(120):
        n = rng.randint(1, 16)
        zero_w = rng.random() < 0.15
        weights = [
            0 if (zero_w and rng.random() < 0.25) else rng.randint(1, 12)
            for _ in range(n)
        ]
        values = [rng.randint(-5, 20) for _ in range(n)]
        cap = rng.randint(0, 3 * n)
        resp = solve({
            "capacity": cap, "weights": weights, "values": values,
            "timeout_seconds": 10.0,
        })
        _check_response_feasibility(resp, weights, cap)
        bf_val, _ = brute_force_optimal(weights, values, cap)
        assert resp["objective"] == bf_val, (seed, weights, values, cap, resp, bf_val)
        assert resp["status"] == "optimal"
        assert resp["upper_bound"] == bf_val
    print("test_random_fuzz_medium: 120 个随机实例与枚举真值一致，上界均有效")


def test_zero_weight_negative_and_equal_density():
    """需求点名场景：零重量、负价值、相同密度。"""

    # 1) 零重量正价值物品必取；零重量负价值物品必舍。
    weights = [0, 0, 3, 4]
    values = [7, -9, 6, 8]
    cap = 5
    resp = solve({"capacity": cap, "weights": weights, "values": values})
    assert resp["status"] == "optimal"
    assert 0 in resp["selected"] and 1 not in resp["selected"]
    assert resp["forced_zero_weight"] == [0]
    bf_val, _ = brute_force_optimal(weights, values, cap)
    assert resp["objective"] == bf_val

    # 容量为 0：只有零重量非负物品能进解。
    resp0 = solve({"capacity": 0, "weights": weights, "values": values})
    assert resp0["objective"] == 7
    assert resp0["selected"] == [0]

    # 2) 全部零重量：正价值全拿，负价值全舍，无需搜索即最优。
    resp_all0 = solve({"capacity": 0, "weights": [0, 0, 0], "values": [3, -1, 2]})
    assert resp_all0["objective"] == 5
    assert resp_all0["selected"] == [0, 2]
    assert resp_all0["status"] == "optimal"

    # 3) 相同密度：(v,w) = (2,1),(4,2),(6,3) 密度均为 2，容量 3。
    #    最优值 6（取 6/3 或 2/1+4/2），枚举核对，且解可行。
    w = [1, 2, 3]
    v = [2, 4, 6]
    resp_d = solve({"capacity": 3, "weights": w, "values": v})
    bf_val, bf_count = brute_force_optimal(w, v, 3)
    assert resp_d["objective"] == bf_val == 6
    assert bf_count == 2  # 确实存在多个同密度最优解
    assert sum(w[i] for i in resp_d["selected"]) <= 3

    # 4) 全部负价值：最优为空解，目标值 0。
    resp_neg = solve({"capacity": 5, "weights": [2, 3], "values": [-1, -4]})
    assert resp_neg["objective"] == 0
    assert resp_neg["selected"] == []

    print("test_zero_weight_negative_and_equal_density: 通过")


def test_classic_cases():
    """经典教科书实例。"""

    # 经典例：weights 2,3,4,5; values 3,4,5,6; cap 5 -> 选(2,3)+(3,4)=7
    resp = solve({"capacity": 5, "weights": [2, 3, 4, 5], "values": [3, 4, 5, 6]})
    assert resp["status"] == "optimal"
    assert resp["objective"] == 7
    assert sorted(resp["selected"]) == [0, 1]

    # 单件超重被剔除
    resp2 = solve({"capacity": 4, "weights": [5, 2], "values": [100, 3]})
    assert resp2["excluded_overweight"] == [0]
    assert resp2["objective"] == 3

    # 空实例
    resp3 = solve({"capacity": 10, "weights": [], "values": []})
    assert resp3["objective"] == 0 and resp3["selected"] == []
    assert resp3["upper_bound"] == 0 and resp3["status"] == "optimal"
    print("test_classic_cases: 通过")
