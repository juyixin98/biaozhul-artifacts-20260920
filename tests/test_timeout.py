"""超时与失败状态语义测试。

验收点：
* 超时返回的解必须真实可行，objective 等于所选子集价值和；
* 超时返回的上界必须有效（>= objective）；
* **绝不把未闭合的间隙当最优**：status == "timeout" 且 UB > obj 时
  relative_gap 必须为正；
* 放宽时限后重算必须能达到 optimal 且最优值一致（可复现）；
* 极短时限下（根节点即检查时间）也必须稳定给出 timeout。
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from knapsack.api import solve  # noqa: E402


def _hard_instance(n, seed=0):
    """强相关“尖峰”实例：w_i 在 [1,n]，v_i = w_i + n/20 附近，
    容量取总重一半——分支定界经典难题，分数上界很紧、剪枝少。"""

    rng = __import__("random").Random(seed)
    weights = [rng.randint(1, n) for _ in range(n)]
    delta = max(1, n // 20)
    values = [w + rng.randint(0, delta) for w in weights]
    capacity = sum(weights) // 2
    return weights, values, capacity


def test_timeout_returns_valid_incumbent_and_bound():
    # 500 件强相关难例给 10ms：该规模在本机稳定无法闭合。
    n, k = 500, 50
    rng = __import__("random").Random(3)
    weights = [rng.randint(1, 1000) for _ in range(n)]
    values = [w + k for w in weights]
    cap = sum(weights) // 2
    resp = solve({
        "capacity": cap, "weights": weights, "values": values,
        "timeout_seconds": 0.01,
    })
    assert resp["ok"]
    assert resp["status"] in ("timeout", "optimal"), resp
    # 可行性与自洽
    sel = resp["selected"]
    assert sum(weights[i] for i in sel) <= cap
    assert sum(values[i] for i in sel) == resp["objective"]
    assert resp["upper_bound"] >= resp["objective"]
    assert resp["relative_gap"] >= 0.0
    assert resp["status"] == "timeout", "该难例在 10ms 内应无法闭合"
    # 未闭合就必须呈现正间隙（除非界恰好证明最优，那种情况会升级 optimal）
    assert resp["upper_bound"] > resp["objective"]
    assert resp["relative_gap"] > 0.0
    print(f"  超时实例：obj={resp['objective']}, UB={resp['upper_bound']}, "
          f"gap={resp['relative_gap']:.4%}, nodes={resp['nodes_explored']}")


def test_tiny_timeout_is_deterministic_timeout():
    """1 微秒时限：预处理本身就会超过时限，根节点时间检查必然触发。

    必须报告 timeout 且呈现正间隙——这是曾经的缺陷回归测试
    （旧实现根节点超时时因栈空而错误升级为 optimal）。"""

    weights, values, cap = _hard_instance(500, seed=11)
    resp = solve({
        "capacity": cap, "weights": weights, "values": values,
        "timeout_seconds": 0.000001,
    })
    assert resp["ok"]
    sel = resp["selected"]
    assert sum(weights[i] for i in sel) <= cap
    assert sum(values[i] for i in sel) == resp["objective"]
    assert resp["upper_bound"] >= resp["objective"]
    assert resp["status"] == "timeout", resp
    assert resp["upper_bound"] > resp["objective"], resp
    assert resp["relative_gap"] > 0.0
    print(f"  1μs 时限：status=timeout, "
          f"obj={resp['objective']}, UB={resp['upper_bound']}, "
          f"gap={resp['relative_gap']:.4%}")


def test_more_time_closes_gap_and_matches():
    """同一实例：更多时间后闭合为 optimal，且最优值不小于超时解。"""

    weights, values, cap = _hard_instance(250, seed=5)
    short = solve({"capacity": cap, "weights": weights, "values": values,
                   "timeout_seconds": 0.005})
    long_ = solve({"capacity": cap, "weights": weights, "values": values,
                   "timeout_seconds": 30.0})
    assert long_["status"] == "optimal", long_
    assert long_["objective"] >= short["objective"]
    # 若短时限恰好也证明了最优，两者必须相等
    if short["status"] == "optimal":
        assert short["objective"] == long_["objective"]
    # 最优值不超过任何曾经给出的上界
    assert long_["objective"] <= short["upper_bound"]
    print(f"  超时解 obj={short['objective']} -> 最优 {long_['objective']}，"
          f"超时上界 {short['upper_bound']} 确实夹住最优值")


def test_timeout_solution_always_feasible_many_seeds():
    """多个不同规模/种子下超时解反复验证可行性（绝不返回超装解）。"""

    bad = 0
    for seed in range(10):
        n = 150 + 20 * seed
        weights, values, cap = _hard_instance(n, seed=seed)
        resp = solve({"capacity": cap, "weights": weights, "values": values,
                      "timeout_seconds": 0.002})
        sel = resp["selected"]
        tw = sum(weights[i] for i in sel)
        tv = sum(values[i] for i in sel)
        assert tw <= cap, (seed, tw, cap)
        assert tv == resp["objective"]
        assert resp["upper_bound"] >= tv
        if resp["status"] == "timeout":
            bad += 1
    print(f"test_timeout_solution_always_feasible_many_seeds: "
          f"10 个实例全部可行自洽（其中 {bad} 个报告 timeout）")
