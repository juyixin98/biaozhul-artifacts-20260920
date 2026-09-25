"""求解器正确性测试：小规模枚举对照、上界有效性、边界情形。"""

import numpy as np
import pytest

from knapsack.brute import brute_force
from knapsack.solver import STATUS_FEASIBLE, STATUS_OPTIMAL, solve


def check_solution_consistency(weights, values, capacity, result):
    """返回值自洽性：选中集合可行、价值与重量一致、上界 >= 价值。"""
    weights = list(weights)
    values = list(values)
    sel = result.selected
    assert len(set(sel)) == len(sel), "selected indices must be unique"
    total_w = sum(weights[i] for i in sel)
    total_v = sum(values[i] for i in sel)
    assert total_w <= capacity, "selected set must be feasible"
    assert total_v == result.value, "reported value must match selected set"
    assert result.upper_bound >= result.value, "upper bound must dominate value"
    if result.status == STATUS_OPTIMAL:
        assert result.gap == 0, "optimal requires closed gap"


class TestKnownInstances:
    def test_textbook_instance(self):
        # 经典实例：最优 15（取物品 0 和 2）
        w = [3, 4, 5]
        v = [6, 7, 9]
        r = solve(w, v, 8)
        assert r.status == STATUS_OPTIMAL
        assert r.value == 15
        assert r.upper_bound == 15
        check_solution_consistency(w, v, 8, r)

    def test_all_items_fit(self):
        w = [1, 2, 3]
        v = [10, 20, 30]
        r = solve(w, v, 100)
        assert r.status == STATUS_OPTIMAL
        assert r.value == 60
        assert r.selected == [0, 1, 2]

    def test_empty_items(self):
        r = solve([], [], 10)
        assert r.status == STATUS_OPTIMAL
        assert r.value == 0
        assert r.selected == []

    def test_zero_capacity(self):
        w = [1, 2]
        v = [5, 6]
        r = solve(w, v, 0)
        assert r.status == STATUS_OPTIMAL
        assert r.value == 0
        assert r.selected == []


class TestEdgeCases:
    def test_zero_weight_positive_value_always_taken(self):
        w = [0, 0, 2]
        v = [5, 7, 4]
        r = solve(w, v, 2)
        assert r.status == STATUS_OPTIMAL
        assert r.value == 16
        assert r.selected == [0, 1, 2]

    def test_zero_weight_nonpositive_value_never_taken(self):
        w = [0, 0, 2]
        v = [0, -3, 4]
        r = solve(w, v, 2)
        assert r.status == STATUS_OPTIMAL
        assert r.value == 4
        assert r.selected == [2]

    def test_zero_capacity_with_zero_weight_items(self):
        w = [0, 1]
        v = [9, 100]
        r = solve(w, v, 0)
        assert r.status == STATUS_OPTIMAL
        assert r.value == 9
        assert r.selected == [0]

    def test_negative_values_excluded(self):
        w = [3, 2, 4]
        v = [-10, 5, -1]
        r = solve(w, v, 9)
        assert r.status == STATUS_OPTIMAL
        assert r.value == 5
        assert r.selected == [1]

    def test_all_negative_values_empty_selection(self):
        w = [1, 1, 1]
        v = [-1, -2, -3]
        r = solve(w, v, 3)
        assert r.status == STATUS_OPTIMAL
        assert r.value == 0
        assert r.selected == []

    def test_equal_density(self):
        # 相同密度 2:1；容量 8 时最优为物品 1+2（重量 8，价值 16）
        w = [2, 3, 5]
        v = [4, 6, 10]
        r = solve(w, v, 8)
        assert r.status == STATUS_OPTIMAL
        assert r.value == 16
        check_solution_consistency(w, v, 8, r)

    def test_equal_density_with_zero_weight(self):
        # 零重量物品与正重量物品密度不可比，预处理必须正确处理
        w = [0, 2, 4]
        v = [3, 4, 8]
        r = solve(w, v, 4)
        assert r.status == STATUS_OPTIMAL
        assert r.value == 11
        assert r.selected == [0, 2]


class TestTimeoutAndBounds:
    def test_node_limit_returns_feasible_with_valid_bound(self):
        rng = np.random.default_rng(42)
        n = 18
        w = rng.integers(1, 20, size=n).tolist()
        v = rng.integers(1, 30, size=n).tolist()
        cap = 60
        opt, _ = brute_force(w, v, cap)
        r = solve(w, v, cap, max_nodes=1)
        assert r.status == STATUS_FEASIBLE
        assert r.gap > 0, "gap must remain open when terminated early"
        assert r.upper_bound >= opt, "upper bound must remain valid under early exit"
        assert r.value <= opt
        check_solution_consistency(w, v, cap, r)

    def test_time_limit_returns_feasible_with_valid_bound(self):
        rng = np.random.default_rng(7)
        n = 20
        w = rng.integers(1, 15, size=n).tolist()
        v = rng.integers(1, 25, size=n).tolist()
        cap = 50
        opt, _ = brute_force(w, v, cap)
        r = solve(w, v, cap, time_limit_sec=0.0)  # 立即超时
        assert r.status == STATUS_FEASIBLE
        assert r.upper_bound >= opt
        check_solution_consistency(w, v, cap, r)

    def test_unclosed_gap_never_reported_optimal(self):
        # 任意提前终止路径都不得报告 optimal
        rng = np.random.default_rng(123)
        n = 16
        w = rng.integers(1, 12, size=n).tolist()
        v = rng.integers(-3, 20, size=n).tolist()
        cap = 30
        for limit in (1, 2, 5, 17):
            r = solve(w, v, cap, max_nodes=limit)
            if r.gap > 0:
                assert r.status == STATUS_FEASIBLE
            else:
                assert r.status == STATUS_OPTIMAL


class TestAgainstBruteForce:
    """随机小规模实例：最优值与暴力枚举一致，上界有效。覆盖零重量/负价值/等密度。"""

    @pytest.mark.parametrize("seed", range(60))
    def test_random_instances(self, seed):
        rng = np.random.default_rng(seed)
        n = int(rng.integers(1, 17))
        # 重量含 0；价值含负数；小取值域制造大量等密度物品
        w = rng.integers(0, 9, size=n).tolist()
        v = rng.integers(-4, 13, size=n).tolist()
        cap = int(rng.integers(0, 25))
        opt, _ = brute_force(w, v, cap)
        r = solve(w, v, cap)
        assert r.status == STATUS_OPTIMAL
        assert r.value == opt, f"seed={seed} w={w} v={v} cap={cap}"
        assert r.upper_bound >= opt
        check_solution_consistency(w, v, cap, r)

    @pytest.mark.parametrize("seed", range(20))
    def test_random_equal_density_heavy(self, seed):
        # 价值恒为重量两倍：所有正重量物品密度相同
        rng = np.random.default_rng(10_000 + seed)
        n = int(rng.integers(1, 15))
        w = rng.integers(1, 6, size=n).tolist()
        v = [2 * x for x in w]
        cap = int(rng.integers(0, 20))
        opt, _ = brute_force(w, v, cap)
        r = solve(w, v, cap)
        assert r.status == STATUS_OPTIMAL
        assert r.value == opt
        check_solution_consistency(w, v, cap, r)

    def test_upper_bound_valid_on_early_exit_random(self):
        # 提前终止时上界仍须 >= 枚举最优值
        rng = np.random.default_rng(999)
        for _ in range(15):
            n = int(rng.integers(10, 17))
            w = rng.integers(0, 10, size=n).tolist()
            v = rng.integers(-3, 15, size=n).tolist()
            cap = int(rng.integers(5, 30))
            opt, _ = brute_force(w, v, cap)
            r = solve(w, v, cap, max_nodes=3)
            assert r.upper_bound >= opt
            assert r.value <= opt
            check_solution_consistency(w, v, cap, r)
