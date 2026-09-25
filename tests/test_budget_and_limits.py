"""预算剪枝与标签上限截断测试。"""

from .helpers import solve, weight_pairs


EDGES = [
    (0, 1, 1, 4),
    (0, 2, 2, 1),
    (1, 3, 1, 1),   # 0-1-3: (2,5)
    (2, 3, 2, 4),   # 0-2-3: (4,5)
    (0, 3, 5, 0),   # (5,0)
]


def test_cost_budget_filters():
    # 费用预算 1：只剩 (5,0)
    resp = solve(EDGES, 0, 3, cost_budget=1)
    assert weight_pairs(resp) == [(5, 0)]
    assert resp["stats"]["pruned_by_budget"] > 0


def test_time_budget_filters():
    # 时间预算 2：只剩 (2,5)
    resp = solve(EDGES, 0, 3, time_budget=2)
    assert weight_pairs(resp) == [(2, 5)]


def test_combined_budgets():
    # 时间 <=4 且费用 <=1：两条 (2,5)/(4,5) 超费用，(5,0) 超时间 ⇒ 无解
    resp = solve(EDGES, 0, 3, time_budget=4, cost_budget=1)
    assert resp["status"] == "unreachable"
    assert resp["paths"] == []


def test_budget_boundary_inclusive_with_tolerance():
    # 预算恰好等于可行权重（2 和 5），容差内视为满足 ⇒ (2,5) 保留
    resp = solve(EDGES, 0, 3, time_budget=2, cost_budget=5)
    assert (2, 5) in weight_pairs(resp)


def test_label_cap_one_returns_truncated():
    # cap=1：只能创建源标签，扩展即截断
    resp = solve(EDGES, 0, 3, label_cap=1)
    assert resp["status"] == "truncated"
    assert resp["stats"]["truncated"] is True
    assert resp["stats"]["label_cap"] == 1


def test_label_cap_small_partial_result():
    # cap 很小时仍应返回已找到的目标标签（部分结果），且自报截断
    resp = solve(EDGES, 0, 3, label_cap=4)
    assert resp["status"] == "truncated"
    assert isinstance(resp["paths"], list)
    # 部分结果中的每条路径仍是合法简单路径
    for p in resp["paths"]:
        assert p["nodes"][0] == 0 and p["nodes"][-1] == 3
        assert len(p["nodes"]) == len(set(p["nodes"]))


def test_cap_greater_than_needed_is_ok():
    resp = solve(EDGES, 0, 3, label_cap=100000)
    assert resp["status"] == "ok"
    assert resp["stats"]["truncated"] is False


def test_undirected_graph():
    # 无向：0-1 (1,10), 1-2 (1,10)，另有 0-2 (3,1) 与 2-1 反向可达
    edges = [(0, 1, 1, 10), (0, 2, 3, 1), (1, 2, 1, 10)]
    resp = solve(edges, 0, 1, directed=False)
    pairs = weight_pairs(resp)
    # 直达 (1,10)；经 2 的 0->2->1 = (4,11) 被支配，不应出现
    assert pairs == [(1, 10)]
