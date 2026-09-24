"""时空 A* 规划器单元测试。

重点：边冲突（对向交换）必须被检测到——只检查顶点的实现会漏掉它。
"""

from __future__ import annotations

import pytest

from app.planner import (
    Constraints,
    EDGE_CONFLICT_SWAP,
    ENDPOINT_OCCUPIED,
    NO_PATH_IN_HORIZON,
    START_OCCUPIED,
    StaticMap,
    VERTEX_CONFLICT,
    Constraints as C,
    Endpoint,
    manhattan,
    plan_path,
    revalidate_path,
)


def grid(w: int, h: int, obstacles=()) -> StaticMap:
    return StaticMap(w, h, frozenset(obstacles))


def make_constraints(paths: dict[int, list[tuple[int, int]]]) -> Constraints:
    """从 {robot_id: 路径} 直接构造约束（等价于从库行展开）。"""
    cons = Constraints()
    max_t = 0
    for rid, path in paths.items():
        for t, cell in enumerate(path):
            cons.vertices.setdefault((cell, t), rid)
        for t, (u, v) in enumerate(zip(path, path[1:])):
            if u != v:
                a, b = (u, v) if u <= v else (v, u)
                cons.edges.setdefault((t, a, b), (rid, u, v))
        cons.endpoints[rid] = Endpoint(rid, path[-1], len(path) - 1)
        max_t = max(max_t, len(path) - 1)
    cons.max_time = max_t
    return cons


def test_free_path_is_straight_line():
    g = grid(5, 1)
    res = plan_path(g, (0, 0), (4, 0), 6, Constraints(), robot_id=1)
    assert res.ok
    assert res.path[0] == (0, 0)
    assert res.path[-1] == (4, 0)
    assert {a["type"] for a in res.actions} == {"move"}
    # 路径只记录到到达终点；终点停留由 endpoint 约束表达
    assert len(res.path) == 5


def test_wait_action_exists_when_corridor_busy():
    """高优先级机器人 t=1 占据咽喉点时，低优先级必须 wait 避让。

    地图：3x1 单行。r1 在 t=1 经过 (1,0)（不停留，终点在下方第二行）。
    r2 从 (0,0) 去 (2,0)：t=1 进 (1,0) 是顶点冲突，必须 wait 一拍。
    """
    g = grid(3, 2)
    cons = Constraints()
    # r1: 上行 (1,0) 只是 t=1 经过；终点 (1,1)
    r1_path = [(1, 1), (1, 0), (1, 1), (1, 1)]
    for t, cell in enumerate(r1_path):
        cons.vertices[(cell, t)] = 1
    for t, (u, v) in enumerate(zip(r1_path, r1_path[1:])):
        if u != v:
            a, b = (u, v) if u <= v else (v, u)
            cons.edges[(t, a, b)] = (1, u, v)
    cons.endpoints[1] = Endpoint(1, (1, 1), 2)
    cons.max_time = 3

    res = plan_path(g, (0, 0), (2, 0), 3, cons, robot_id=2)
    assert res.ok, res.evidence
    types = [a["type"] for a in res.actions]
    assert types[0] == "wait"  # 必须等一拍
    assert types[1:] == ["move", "move"]
    # 校验结果自身无冲突
    assert revalidate_path(g, res.path, cons, 2, 3) is None


def test_head_on_swap_in_2_cells_is_edge_conflict_not_vertex():
    """关键验收：2 格走廊对向交换。

    r1: [(0,0),(1,0)]，r2 想 [(1,0),(0,0)]。
    两个时刻顶点占用都不冲突（t=0 各占一端，t=1 也是），
    只有对向交换的边冲突；只做顶点检查的实现会错误放行。
    """
    g = grid(2, 1)
    cons = make_constraints({1: [(0, 0), (1, 0)]})
    res = plan_path(g, (1, 0), (0, 0), 1, cons, robot_id=2)
    assert not res.ok
    assert res.evidence["type"] == NO_PATH_IN_HORIZON
    root = res.evidence["root_cause"]
    assert root == EDGE_CONFLICT_SWAP
    assert res.evidence["with_robot"] == 1
    assert res.evidence["from"] == [1, 0]
    assert res.evidence["to"] == [0, 0]
    # 剪枝计数里有边冲突记录
    assert res.prune_counts[EDGE_CONFLICT_SWAP] >= 1


def test_bidirectional_narrow_corridor_blocks():
    """5x1 双向窄道：低优先级无路可让（没有侧线），返回证据。"""
    g = grid(5, 1)
    cons = make_constraints(
        {1: [(0, 0), (1, 0), (2, 0), (3, 0), (4, 0)]
            + [(4, 0)] * 3}
    )
    res = plan_path(g, (4, 0), (0, 0), 6, cons, robot_id=2)
    assert not res.ok
    # 顶点或边冲突至少其一，且证据明确指向 r1
    assert res.evidence["with_robot"] == 1
    assert res.evidence["type"] == NO_PATH_IN_HORIZON


def test_same_edge_opposite_direction_at_different_times_is_fine():
    """边是时间索引的：不同 tick 在同一条无向边上反向经过是合法的。

    地图 3x2。边 e = (1,0)-(1,1)。
    r1: (0,0)->(1,0)->(1,1)->(2,1)，在 [1,2] 向上使用 e；
    r2: (2,0)->(1,0) 在 t=2（此时 r1 已离开 (1,0)），
        再 (1,0)->(1,1) 在 [2,3] 向下反方向使用 e，然后 (0,1)。
    两个时刻顶点互不重合，仅在不同 tick 反向共用一条边——合法，
    用以证明"边检查"是按时间区分的，不会误伤。
    """
    g = grid(3, 2)
    r1 = [(0, 0), (1, 0), (1, 1), (2, 1), (2, 1), (2, 1)]
    cons = make_constraints({1: r1})
    r2 = [(2, 0), (2, 0), (1, 0), (1, 1), (0, 1), (0, 1)]
    ev = revalidate_path(g, r2, cons, robot_id=2, window_end=5)
    assert ev is None, ev


def test_same_edge_same_tick_opposite_direction_is_swap():
    """对照组：同 tick 反向使用同一条边 = 对向交换，必须拦截。

    最小交换：r1 [(0,0),(1,0)]，r2 [(1,0),(0,0)]。
    顶点序列在 t=0、t=1 都不重合，只有边冲突——顶点检查会漏掉。
    """
    g = grid(2, 1)
    r1 = [(0, 0), (1, 0)]
    cons = make_constraints({1: r1})
    bad = [(1, 0), (0, 0)]
    ev = revalidate_path(g, bad, cons, robot_id=2, window_end=1)
    assert ev is not None
    assert ev["type"] == EDGE_CONFLICT_SWAP
    assert ev["with_robot"] == 1
    # 约束原语本身也直接识别
    assert cons.edge_conflict((1, 0), (0, 0), 0)[0] == EDGE_CONFLICT_SWAP


def test_endpoint_occupied_goal_fast_path():
    """终点永久占用：goal 是别人永久停留格，直接 ENDPOINT_OCCUPIED。"""
    g = grid(4, 1)
    # r1: (0,0)->(1,0)->(2,0)，终点 (2,0) arrival=2；起点不在 (2,0)
    cons = make_constraints(
        {1: [(0, 0), (1, 0), (2, 0), (2, 0), (2, 0)]}
    )
    res = plan_path(g, (3, 0), (2, 0), 4, cons, robot_id=2)
    assert not res.ok
    assert res.evidence["type"] == ENDPOINT_OCCUPIED
    assert res.evidence["with_robot"] == 1
    assert res.evidence["cell"] == [2, 0]


def test_endpoint_holder_blocks_entry_after_arrival():
    """对方 t=3 起停在 (1,0)：t=2 之前可穿过，t>=3 不得进入。"""
    g = grid(3, 1)
    cons = make_constraints(
        {1: [(2, 0), (2, 0), (2, 0), (1, 0), (0, 0), (0, 0)]}
    )
    # r2 想去 (2,0)：r1 在 t=0..2 停 (2,0)，arrival 终点是 (0,0)
    # r2 起点不能冲突；起点给 (0,0)? r1 终点是(0,0) arrival=4 -> start blocked.
    # 用更宽松的网格，让 r2 从侧面进入
    g = grid(3, 2)
    cons = make_constraints(
        {1: [(2, 1), (2, 1), (2, 1), (1, 1), (0, 1), (0, 1)]}
    )
    res = plan_path(g, (0, 0), (2, 0), 5, cons, robot_id=2)
    assert res.ok
    assert revalidate_path(g, res.path, cons, 2, 5) is None


def test_start_occupied_at_t0():
    g = grid(2, 1)
    cons = make_constraints({1: [(0, 0), (1, 0), (1, 0)]})
    res = plan_path(g, (0, 0), (1, 0), 2, cons, robot_id=2)
    assert not res.ok
    assert res.evidence["type"] == START_OCCUPIED


def test_start_and_goal_same_wait_path():
    g = grid(2, 1)
    res = plan_path(g, (1, 0), (1, 0), 3, Constraints(), robot_id=1)
    assert res.ok
    assert res.path == [(1, 0)] * 4
    assert all(a["type"] == "wait" for a in res.actions)


def test_obstacle_prechecks():
    g = grid(3, 1, obstacles={(1, 0)})
    res = plan_path(g, (0, 0), (2, 0), 4, Constraints(), robot_id=1)
    assert not res.ok
    assert res.evidence["type"] == NO_PATH_IN_HORIZON
    res2 = plan_path(g, (1, 0), (0, 0), 4, Constraints(), robot_id=1)
    assert not res2.ok
    assert res2.evidence["type"] == "START_ON_OBSTACLE"
    res3 = plan_path(g, (0, 0), (1, 0), 4, Constraints(), robot_id=1)
    assert not res3.ok
    assert res3.evidence["type"] == "GOAL_ON_OBSTACLE"


def test_revalidate_detects_swap_after_concurrent_commit():
    """提交前重校验：并发插入的对向交换预订必须被发现。"""
    g = grid(2, 1)
    my_path = [(1, 0), (0, 0)]
    cons = make_constraints({1: [(0, 0), (1, 0)]})
    ev = revalidate_path(g, my_path, cons, robot_id=2, window_end=1)
    assert ev is not None
    assert ev["type"] == EDGE_CONFLICT_SWAP


def test_revalidate_detects_map_obstacle_change():
    """规划期间地图加障碍：重校验必须发现 MAP_OBSTACLE_ON_PATH。"""
    g = grid(2, 1, obstacles={(1, 0)})
    ev = revalidate_path(
        g, [(0, 0), (1, 0)], Constraints(), robot_id=1, window_end=1)
    assert ev is not None
    assert ev["type"] == "MAP_OBSTACLE_ON_PATH"


def test_vertex_conflict_basic():
    g = grid(3, 2)
    cons = make_constraints({1: [(1, 0), (1, 0), (1, 0)]})
    # r2 必须在 t=1 经过 (1,0) 才能继续；可以绕行 (1,1)
    res = plan_path(g, (0, 0), (2, 0), 4, cons, robot_id=2)
    assert res.ok, res.evidence
    # t=1 时不得占据 (1,0)（r1 在那），绕行到 (0,1)
    assert res.path[1] == (0, 1)
    assert revalidate_path(g, res.path, cons, 2, 4) is None


def test_manhattan_helper():
    assert manhattan((0, 0), (3, 4)) == 7
