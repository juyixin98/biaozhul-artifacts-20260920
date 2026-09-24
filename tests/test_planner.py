"""规划器算法层测试：直接针对空间-时间 A*，重点覆盖边冲突与失败证据。"""

from __future__ import annotations

import pytest

from app.planner import (
    Constraints,
    EdgeBlock,
    GridMap,
    PermanentBlock,
    VertexBlock,
    constraints_from_plans,
    plan_one,
    prioritized_batch,
    validate_path,
)


def corridor4():
    return GridMap(4, 1, [])


def make_b_constraints(b_path):
    cons = Constraints()
    for k, c in enumerate(b_path):
        cons.vertex.add(VertexBlock(c, k, "B"))
    for k, (s, d) in enumerate(zip(b_path, b_path[1:])):
        cons.edges.add(EdgeBlock(s, d, k, "B"))
    cons.permanent.add(PermanentBlock(b_path[-1], len(b_path) - 1, "B"))
    return cons


def test_open_grid_shortest_wait_and_move_actions():
    grid = GridMap(6, 6, [])
    res = plan_one(grid, (0, 0), (4, 3), Constraints(), horizon=30)
    assert res.found
    assert res.path[0] == (0, 0)
    assert res.path[-1] == (4, 3)
    # 最优长度 = 曼哈顿距离 + 1
    assert len(res.path) == 4 + 3 + 1
    # 每一步要么等待要么移动一格
    for a, b in zip(res.path, res.path[1:]):
        assert (a == b) or abs(a[0] - b[0]) + abs(a[1] - b[1]) == 1
    # 无障碍环境不需要等待
    assert all(a != b for a, b in zip(res.path, res.path[1:]))


def test_vertex_conflict_detected():
    # A 想在 t=1 到达 B 在 t=1 占据的格子 -> 顶点冲突，等待一回合再走即可
    grid = GridMap(4, 1, [])
    cons = Constraints()
    cons.vertex.add(VertexBlock((1, 0), 1, "B"))
    res = plan_one(grid, (0, 0), (3, 0), cons, horizon=20)
    assert res.found
    assert res.path[1] == (0, 0)  # t=1 等待
    assert res.path[-1] == (3, 0)
    # 复核必须通过
    assert validate_path(grid, (0, 0), res.path, cons) is None


def test_edge_swap_conflict_must_be_detected():
    """对向交换：1x4 走廊，B 从右端走到左端。真实规划器（边检查开）必须判失败，
    且证据里必须包含 type=edge 的边交换证据——不能只报顶点冲突。"""
    grid = corridor4()
    b_path = [(3, 0), (2, 0), (1, 0), (0, 0)]
    cons = make_b_constraints(b_path)

    res = plan_one(grid, (0, 0), (3, 0), cons, horizon=30, actor_ref="A")
    assert not res.found
    blockers = res.evidence.blockers
    edge_blocks = [b for b in blockers if b["type"] == "edge"]
    assert edge_blocks, f"失败证据中缺少边交换证据：{blockers}"
    # 每个边证据都确实是同刻反向穿越
    for b in edge_blocks:
        assert b["src"] != b["dst"]


def test_naive_vertex_only_planner_misses_swap():
    """反证：关掉边检查的"天真"规划器会把交换路径当成可行解输出；
    独立复核器必须抓到这条边冲突。这条测试保证验收不是形式主义。"""
    grid = corridor4()
    b_path = [(3, 0), (2, 0), (1, 0), (0, 0)]
    cons = make_b_constraints(b_path)

    naive = plan_one(
        grid, (0, 0), (3, 0), cons, horizon=30, enforce_edges=False, actor_ref="A"
    )
    assert naive.found  # 天真规划器认为有解
    # 但任何时刻两机器人都没有占同一格——顶点表看不出来：
    b_occ = {k: c for k, c in enumerate(b_path)}
    for k, c in enumerate(naive.path):
        assert b_occ[k] != c
    # 独立复核必须判其非法，且类型是 edge
    verdict = validate_path(grid, (0, 0), naive.path, cons)
    assert verdict is not None and verdict["type"] == "edge"


def test_permanent_goal_blocks():
    grid = corridor4()
    cons = Constraints()
    cons.permanent.add(PermanentBlock((3, 0), 0, "B"))
    res = plan_one(grid, (0, 0), (3, 0), cons, horizon=15)
    assert not res.found
    assert res.evidence.kind == "permanent_goal"
    assert res.evidence.blockers[0]["type"] == "permanent"


def test_permanent_goal_from_some_time_on():
    # B 在 t=2 之后永久停在 (3,0)，A 不可能以那里为终点
    grid = corridor4()
    cons = Constraints()
    cons.permanent.add(PermanentBlock((3, 0), 2, "B"))
    res = plan_one(grid, (0, 0), (3, 0), cons, horizon=15)
    assert not res.found
    assert res.evidence.kind == "permanent_goal"


def test_waiting_behind_then_pass_after_goal_left_impossible():
    """永久停留意味着终点不会再腾出：B 在 t=1 到达 (3,0) 并永久停留，
    A 的终点是 (3,0) 时无解；但 A 终点在 (2,0) 且能在 t=1 之前到则可行。"""
    grid = corridor4()
    cons = Constraints()
    cons.vertex.add(VertexBlock((2, 0), 0, "B"))  # B t=0 在 (2,0)
    cons.vertex.add(VertexBlock((3, 0), 1, "B"))
    cons.permanent.add(PermanentBlock((3, 0), 1, "B"))
    res = plan_one(grid, (0, 0), (3, 0), cons, horizon=20)
    assert not res.found


def test_prioritized_batch_fixed_priority_is_incomplete():
    """固定优先级贪婪：高优先级（id 小者先处理）占用走廊，低优先级判失败。
    失败证据指向被高优先级者封死的边/顶点。"""
    grid = corridor4()
    reqs = [("A", (0, 0), (3, 0)), ("B", (3, 0), (0, 0))]
    plans, failure = prioritized_batch(grid, reqs, Constraints(), horizon=30)
    assert plans is None
    assert failure.robot_id == "B"
    # 证据必须提到 A，且包含边交换阻挡
    refs = [b["blocker_ref"] for b in failure.evidence.blockers]
    assert any(r == "A" for r in refs)
    assert any(b["type"] == "edge" for b in failure.evidence.blockers)


def test_prioritized_batch_succeeds_when_space_allows():
    # 3x1 走廊，但目标不交换：A (0,0)->(1,0)，B (2,0)->(2,0)（已在终点，纯等待）
    grid = GridMap(3, 1, [])
    reqs = [("A", (0, 0), (1, 0)), ("B", (2, 0), (2, 0))]
    plans, failure = prioritized_batch(grid, reqs, Constraints(), horizon=10)
    assert failure is None
    assert [p.robot_id for p in plans] == ["A", "B"]
    # 合并复核
    cons = constraints_from_plans([(p.robot_id, p.path) for p in plans], 0)
    for p, (_, s, _) in zip(plans, reqs):
        assert validate_path(grid, s, p.path, Constraints()) is None
    # 每刻顶点表无重复
    occ = {}
    for p in plans:
        for k, c in enumerate(p.path):
            occ.setdefault(k, []).append(c)
    assert all(len(cells) == len(set(cells)) for cells in occ.values())


def test_bay_allows_opposing_traffic_to_pass():
    """带会让湾的窄道（默认地图的缩影）：两机器人相向而行，借助港湾错车可以同时通过，
    边冲突被绕行而非交换解决。"""
    obstacles = [(x, 1) for x in range(1, 7) if x != 2] + [
        (x, 3) for x in range(1, 7) if x != 5
    ]
    grid = GridMap(8, 5, obstacles)
    reqs = [("R1", (0, 0), (7, 0)), ("R8", (7, 4), (0, 4))]
    plans, failure = prioritized_batch(grid, reqs, Constraints(), horizon=80)
    assert failure is None, [b for b in failure.evidence.blockers[:5]] if failure else None

    # 独立模拟：逐刻检查既无同格也无对向交换
    paths = {p.robot_id: p.path for p in plans}
    max_t = max(len(p) for p in paths.values())
    a, b = paths["R1"], paths["R8"]
    for t in range(max_t):
        ca = a[t] if t < len(a) else a[-1]
        cb = b[t] if t < len(b) else b[-1]
        assert ca != cb, f"t={t} 顶点冲突 {ca}"
        if 0 <= t < len(a) - 1 and 0 <= t < len(b) - 1:
            sa, da = a[t], a[t + 1]
            sb, db = b[t], b[t + 1]
            assert not (sa == db and da == sb), f"t={t} 对向交换"


def test_start_blocked_returns_evidence():
    grid = corridor4()
    cons = Constraints()
    cons.vertex.add(VertexBlock((0, 0), 0, "B"))
    res = plan_one(grid, (0, 0), (3, 0), cons, horizon=10)
    assert not res.found
    assert res.evidence.kind == "start_blocked"


@pytest.mark.parametrize(
    "bad_path",
    [
        [],
        [(1, 0), (2, 0)],
        [(0, 0), (2, 0)],  # 跳跃，非单步
        [(0, 0), (0, 1)],  # 走到障碍（在 4x1 地图上越界）
    ],
)
def test_validate_path_rejects_malformed(bad_path):
    grid = corridor4()
    verdict = validate_path(grid, (0, 0), bad_path, Constraints())
    assert verdict is not None
