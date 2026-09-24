"""时刻表校验器测试。"""

from mrcbs.grid import parse_grid
from mrcbs.validate import validate_timetable

GRID = parse_grid(["...", "...", "..."])
STARTS = [(1, 0), (0, 1)]
GOALS = [(1, 2), (2, 1)]


def test_valid_timetable():
    # A0 等待一步让 A1 先过中心
    tt = [
        [(1, 0), (1, 0), (1, 1), (1, 2)],
        [(0, 1), (1, 1), (2, 1), (2, 1)],
    ]
    assert validate_timetable(GRID, STARTS, GOALS, tt) == []


def test_detects_vertex_conflict():
    tt = [
        [(1, 0), (1, 1), (1, 2)],
        [(0, 1), (1, 1), (2, 1)],
    ]
    violations = validate_timetable(GRID, STARTS, GOALS, tt)
    assert any("顶点冲突" in v for v in violations)


def test_detects_edge_conflict():
    # 两机器人在 t=0->1 对向换边
    starts = [(0, 0), (0, 1)]
    goals = [(0, 1), (0, 0)]
    tt = [
        [(0, 0), (0, 1)],
        [(0, 1), (0, 0)],
    ]
    violations = validate_timetable(GRID, starts, goals, tt)
    assert any("边冲突" in v for v in violations)


def test_detects_leaving_goal():
    tt = [
        [(1, 0), (1, 1), (1, 2), (1, 1)],
        [(0, 1), (0, 1), (1, 1), (2, 1)],
    ]
    violations = validate_timetable(GRID, STARTS, GOALS, tt)
    assert any("离开目标" in v for v in violations)


def test_detects_illegal_move():
    tt = [
        [(1, 0), (1, 2)],
        [(0, 1), (2, 1)],
    ]
    violations = validate_timetable(GRID, STARTS, GOALS, tt)
    assert any("非法移动" in v for v in violations)


def test_detects_obstacle():
    grid = parse_grid([".#."])
    tt = [[(0, 0), (0, 1), (0, 2)]]
    violations = validate_timetable(grid, [(0, 0)], [(0, 2)], tt)
    assert any("障碍" in v for v in violations)
