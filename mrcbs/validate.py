"""时刻表校验器：检查路径合法性、顶点/边冲突与目标持续占格。"""

from __future__ import annotations

import numpy as np

from .grid import Cell, is_free


def validate_timetable(
    grid: np.ndarray,
    starts: list[Cell],
    goals: list[Cell],
    timetable: list[list[Cell]],
) -> list[str]:
    """返回违规描述列表，空列表表示完全合法。"""
    violations: list[str] = []
    n = len(starts)
    if len(timetable) != n:
        return [f"时刻表机器人数量 {len(timetable)} 与期望 {n} 不符"]
    horizon = max((len(p) for p in timetable), default=0)
    if horizon == 0:
        return ["时刻表为空"]

    def pos(i: int, t: int) -> Cell:
        p = timetable[i]
        return p[min(t, len(p) - 1)]

    for i in range(n):
        path = timetable[i]
        if not path:
            violations.append(f"机器人 {i}: 路径为空")
            continue
        if path[0] != starts[i]:
            violations.append(f"机器人 {i}: 起点 {path[0]} != 期望 {starts[i]}")
        if path[-1] != goals[i]:
            violations.append(f"机器人 {i}: 终点 {path[-1]} != 目标 {goals[i]}")
        for t, cell in enumerate(path):
            if not is_free(grid, cell):
                violations.append(f"机器人 {i}: t={t} 位于障碍或界外 {cell}")
            if t > 0:
                dr = abs(cell[0] - path[t - 1][0])
                dc = abs(cell[1] - path[t - 1][1])
                if dr + dc > 1:
                    violations.append(
                        f"机器人 {i}: t={t - 1}->{t} 非法移动 {path[t - 1]}->{cell}")
        # 到达目标后必须持续占格
        try:
            arrival = path.index(goals[i])
        except ValueError:
            violations.append(f"机器人 {i}: 从未到达目标 {goals[i]}")
            continue
        for t in range(arrival, len(path)):
            if path[t] != goals[i]:
                violations.append(
                    f"机器人 {i}: 到达后 t={t} 离开目标 {path[t]}")
                break

    for t in range(horizon):
        for i in range(n):
            for j in range(i + 1, n):
                if pos(i, t) == pos(j, t):
                    violations.append(
                        f"顶点冲突: 机器人 {i},{j} 于 t={t} 同在 {pos(i, t)}")
                if (pos(i, t) == pos(j, t + 1) and pos(j, t) == pos(i, t + 1)
                        and pos(i, t) != pos(j, t)):
                    violations.append(
                        f"边冲突: 机器人 {i},{j} 于 t={t}->{t + 1} 对向换边")
    return violations
