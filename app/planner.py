"""时空 A* 规划器（space-time A* / CBS 中单智能体搜索的经典做法）。

冲突模型（离散时间、4 邻域，每 tick 只能 wait 或移动一格）：

1. 顶点冲突 vertex conflict：两个机器人在同一时刻 t 占据同一格。
2. 边冲突 edge conflict —— 对向交换 swap：机器人 A 在区间 [t, t+1]
   从 u 走到 v，而机器人 B 同时从 v 走到 u。即使每个时刻顶点都不重合，
   物理上两机仍会在窄道中相撞。只检查顶点会漏掉它，必须单独检查边。
3. 终点占用 endpoint hold：机器人到达终点后永久停在该格
   （对 t >= arrival 恒成立），后续规划必须绕开或推迟进入。

说明：本服务采用**固定优先级**（按机器人编号 1..8 升序）顺序规划，
先到先得的已预订路径被视为硬约束，低优先级机器人为其让路。
该策略不保证全局完备——高优先级机器人的选择可能封死低优先级机器人，
即使存在一个所有机器人都能通行的联合解。此时服务如实返回冲突证据，
由调用方撤销/调整高优先级预订后重试。
"""

from __future__ import annotations

import heapq
from dataclasses import dataclass, field
from typing import Any

Cell = tuple[int, int]

# 冲突类型常量
VERTEX_CONFLICT = "VERTEX_CONFLICT"
EDGE_CONFLICT_SWAP = "EDGE_CONFLICT_SWAP"
EDGE_CONFLICT_SAME_DIRECTION = "EDGE_CONFLICT_SAME_DIRECTION"
ENDPOINT_OCCUPIED = "ENDPOINT_OCCUPIED"
START_OCCUPIED = "START_OCCUPIED"
GOAL_ON_OBSTACLE = "GOAL_ON_OBSTACLE"
START_ON_OBSTACLE = "START_ON_OBSTACLE"
OUT_OF_BOUNDS = "OUT_OF_BOUNDS"
NO_PATH_IN_HORIZON = "NO_PATH_IN_HORIZON"
MAP_OBSTACLE_ON_PATH = "MAP_OBSTACLE_ON_PATH"

DIRECTIONS = ((1, 0), (-1, 0), (0, 1), (0, -1))


def manhattan(a: Cell, b: Cell) -> int:
    return abs(a[0] - b[0]) + abs(a[1] - b[1])


@dataclass(frozen=True)
class StaticMap:
    width: int
    height: int
    obstacles: frozenset[Cell]

    def in_bounds(self, cell: Cell) -> bool:
        x, y = cell
        return 0 <= x < self.width and 0 <= y < self.height

    def passable(self, cell: Cell) -> bool:
        return self.in_bounds(cell) and cell not in self.obstacles

    def neighbors(self, cell: Cell) -> list[Cell]:
        """wait 由调用方单独处理；这里返回 4 邻域中地图上可通行的格。"""
        x, y = cell
        out: list[Cell] = []
        for dx, dy in DIRECTIONS:
            nxt = (x + dx, y + dy)
            if self.passable(nxt):
                out.append(nxt)
        return out


@dataclass
class Endpoint:
    """某机器人的终点停留：自 arrival 起永久占据 cell。"""

    robot_id: int
    cell: Cell
    arrival: int


@dataclass
class Constraints:
    """规划某个机器人时，其他机器人已生效预订构成的时空约束快照。"""

    # (cell, t) -> 占用该格的机器人 id
    vertices: dict[tuple[Cell, int], int] = field(default_factory=dict)
    # (t, 端点较小格, 端点较大格) -> (robot_id, u, v)，区间 [t,t+1] 的有向移动
    edges: dict[tuple[int, Cell, Cell], tuple[int, Cell, Cell]] = field(
        default_factory=dict
    )
    # robot_id -> 终点停留
    endpoints: dict[int, Endpoint] = field(default_factory=dict)
    # 全部已有预订覆盖到的最大 tick（规划窗口末端）
    max_time: int = 0

    def endpoint_holder(self, cell: Cell) -> Endpoint | None:
        for ep in self.endpoints.values():
            if ep.cell == cell:
                return ep
        return None

    def vertex_robot(self, cell: Cell, t: int) -> int | None:
        return self.vertices.get((cell, t))

    def edge_conflict(
        self, u: Cell, v: Cell, t: int
    ) -> tuple[str, int, Cell, Cell] | None:
        """返回 (冲突类型, 对方机器人, 对方起点, 对方终点)；无冲突返回 None。"""
        a, b = (u, v) if u <= v else (v, u)
        other = self.edges.get((t, a, b))
        if other is None:
            return None
        robot_id, ou, ov = other
        if ou == v and ov == u:
            return EDGE_CONFLICT_SWAP, robot_id, ou, ov
        # 同方向使用同一条边意味着两端顶点之一必然重合，属顶点级碰撞，
        # 这里仍显式拦截作为纵深防御。
        return EDGE_CONFLICT_SAME_DIRECTION, robot_id, ou, ov


@dataclass
class PlanResult:
    ok: bool
    path: list[Cell] = field(default_factory=list)
    actions: list[dict[str, Any]] = field(default_factory=list)
    evidence: dict[str, Any] | None = None
    nodes_explored: int = 0
    prune_counts: dict[str, int] = field(default_factory=dict)


def _evidence(
    conflict_type: str,
    message: str,
    *,
    cell: Cell | None = None,
    conflict_time: int | None = None,
    with_robot: int | None = None,
    frm: Cell | None = None,
    to: Cell | None = None,
    horizon: int | None = None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    ev: dict[str, Any] = {"type": conflict_type, "message": message}
    if cell is not None:
        ev["cell"] = list(cell)
    if conflict_time is not None:
        ev["conflict_time"] = conflict_time
    if with_robot is not None:
        ev["with_robot"] = with_robot
    if frm is not None:
        ev["from"] = list(frm)
    if to is not None:
        ev["to"] = list(to)
    if horizon is not None:
        ev["horizon"] = horizon
    if extra:
        ev.update(extra)
    return ev


def _blocked_reason(
    c: Constraints,
    frm: Cell,
    to: Cell,
    t_next: int,
    robot_id: int,
) -> dict[str, Any] | None:
    """检查一次 wait/move 转移在 t_next 时刻是否合法；不合法返回证据。"""
    # 1) 顶点冲突
    other = c.vertex_robot(to, t_next)
    if other is not None and other != robot_id:
        return _evidence(
            VERTEX_CONFLICT,
            f"cell {to} is occupied by robot {other} at t={t_next}",
            cell=to,
            conflict_time=t_next,
            with_robot=other,
        )
    # 2) 终点停留冲突（对方自 arrival 起永久占据）
    ep = c.endpoint_holder(to)
    if ep is not None and ep.robot_id != robot_id and ep.arrival <= t_next:
        return _evidence(
            ENDPOINT_OCCUPIED,
            f"cell {to} is the permanent endpoint of robot {ep.robot_id} "
            f"since t={ep.arrival}",
            cell=to,
            conflict_time=t_next,
            with_robot=ep.robot_id,
        )
    # 3) 边冲突（wait 不占边）
    if frm != to:
        edge = c.edge_conflict(frm, to, t_next - 1)
        if edge is not None:
            etype, erobot, eu, ev_cell = edge
            return _evidence(
                etype,
                f"opposing swap on edge {frm}<->{to} with robot {erobot} "
                f"during [{t_next - 1},{t_next}]",
                cell=to,
                conflict_time=t_next - 1,
                with_robot=erobot,
                frm=frm,
                to=to,
            )
    return None


def _stay_feasible(
    c: Constraints, cell: Cell, t_from: int, t_to: int, robot_id: int
) -> int | None:
    """在 cell 上从 t_from 停留到 t_to 是否合法；返回首个冲突时刻或 None。"""
    for t in range(t_from + 1, t_to + 1):
        other = c.vertex_robot(cell, t)
        if other is not None and other != robot_id:
            return t
        ep = c.endpoint_holder(cell)
        if ep is not None and ep.robot_id != robot_id and ep.arrival <= t:
            return t
    return None


def _actions_from_path(path: list[Cell]) -> list[dict[str, Any]]:
    actions: list[dict[str, Any]] = []
    for t, (frm, to) in enumerate(zip(path, path[1:])):
        actions.append(
            {
                "tick": t,
                "type": "wait" if frm == to else "move",
                "from": list(frm),
                "to": list(to),
            }
        )
    return actions


def plan_path(
    grid: StaticMap,
    start: Cell,
    goal: Cell,
    horizon: int,
    constraints: Constraints,
    robot_id: int,
) -> PlanResult:
    """在 [0, horizon] 窗口内搜索时空路径。

    终点判定：到达 goal 且能在 goal 上合法停留至窗口末端（其他机器人的
    终点停留是永久的，不在窗口内也会挡住终点）。
    """
    prune_counts: dict[str, int] = {}

    def record_prune(ev: dict[str, Any]) -> None:
        prune_counts[ev["type"]] = prune_counts.get(ev["type"], 0) + 1

    # ---- 静态检查：起点/终点必须在界内且非障碍 ----
    if not grid.in_bounds(start):
        return PlanResult(False, evidence=_evidence(
            OUT_OF_BOUNDS, f"start {start} is out of bounds", cell=start))
    if start in grid.obstacles:
        return PlanResult(False, evidence=_evidence(
            START_ON_OBSTACLE, f"start {start} is an obstacle", cell=start))
    if not grid.in_bounds(goal):
        return PlanResult(False, evidence=_evidence(
            OUT_OF_BOUNDS, f"goal {goal} is out of bounds", cell=goal))
    if goal in grid.obstacles:
        return PlanResult(False, evidence=_evidence(
            GOAL_ON_OBSTACLE, f"goal {goal} is an obstacle", cell=goal))
    if horizon < 0:
        return PlanResult(False, evidence=_evidence(
            NO_PATH_IN_HORIZON, "negative horizon", horizon=horizon))

    # ---- t=0 起点占用检查（含已在终点停留的机器人）----
    other = constraints.vertex_robot(start, 0)
    if other is not None and other != robot_id:
        return PlanResult(False, evidence=_evidence(
            START_OCCUPIED,
            f"start {start} is occupied by robot {other} at t=0",
            cell=start, conflict_time=0, with_robot=other))
    ep = constraints.endpoint_holder(start)
    if ep is not None and ep.robot_id != robot_id and ep.arrival <= 0:
        return PlanResult(False, evidence=_evidence(
            START_OCCUPIED,
            f"start {start} is permanently held by robot {ep.robot_id}",
            cell=start, conflict_time=0, with_robot=ep.robot_id))

    # ---- 终点永久占用：快速给出最有解释力的证据 ----
    ep = constraints.endpoint_holder(goal)
    if ep is not None and ep.robot_id != robot_id:
        return PlanResult(
            False,
            prune_counts=prune_counts,
            evidence=_evidence(
                ENDPOINT_OCCUPIED,
                f"goal {goal} is permanently occupied by robot {ep.robot_id} "
                f"since t={ep.arrival}; fixed-priority planning cannot evict it",
                cell=goal,
                conflict_time=max(ep.arrival, 0),
                with_robot=ep.robot_id,
                extra={"fixed_priority": True, "horizon": horizon},
            ),
        )

    # start == goal：只要能一直等到窗口末端即可（路径记为纯 wait 序列，
    # 长度到 horizon 表示该机器人在整个窗口内停留）
    if start == goal:
        bad_t = _stay_feasible(constraints, start, 0, horizon, robot_id)
        if bad_t is None:
            path = [start] * (horizon + 1)
            return PlanResult(True, path=path,
                              actions=_actions_from_path(path),
                              prune_counts=prune_counts)
        other2 = constraints.vertex_robot(start, bad_t)
        return PlanResult(False, prune_counts=prune_counts, evidence=_evidence(
            VERTEX_CONFLICT,
            f"cannot hold start=goal {start}: occupied at t={bad_t}",
            cell=start, conflict_time=bad_t, with_robot=other2))
    # ---- 时空 A* ----
    # 状态 (cell, t)；f = g(=t) + h(manhattan)
    counter = 0
    open_heap: list[tuple[int, int, int, Cell]] = []
    heapq.heappush(open_heap, (manhattan(start, goal), counter, 0, start))
    came_from: dict[tuple[Cell, int], tuple[Cell, int] | None] = {(start, 0): None}
    g_score: dict[tuple[Cell, int], int] = {(start, 0): 0}
    nodes_explored = 0

    # 离终点最近的一次被剪枝（h 越小越好，平手取 t 更大），作为失败证据
    best_prune: tuple[int, int, dict[str, Any]] | None = None

    def consider(nxt: Cell, t_next: int, ev: dict[str, Any]) -> None:
        nonlocal best_prune
        record_prune(ev)
        key = (manhattan(nxt, goal), -t_next)
        cand = (key[0], t_next, ev)
        if best_prune is None or (cand[0], -cand[1]) < (
            best_prune[0],
            -best_prune[1],
        ):
            best_prune = cand

    while open_heap:
        _f, _c, t, cell = heapq.heappop(open_heap)
        if g_score.get((cell, t)) != t:
            continue  # 过期堆项
        nodes_explored += 1

        # 终点判定：到达 goal，且自 t 起能在 goal 上合法停留到窗口末端。
        # 路径本身不填充 wait，终点之后的永久停留由 endpoint 约束表示。
        if cell == goal:
            bad_t = _stay_feasible(constraints, goal, t, horizon, robot_id)
            if bad_t is None:
                # 回溯
                rev: list[Cell] = []
                cur: tuple[Cell, int] | None = (cell, t)
                while cur is not None:
                    rev.append(cur[0])
                    cur = came_from[cur]
                rev.reverse()
                path = rev
                return PlanResult(
                    True,
                    path=path,
                    actions=_actions_from_path(path),
                    nodes_explored=nodes_explored,
                    prune_counts=prune_counts,
                )
            other2 = constraints.vertex_robot(goal, bad_t)
            ev = _evidence(
                VERTEX_CONFLICT,
                f"reached goal {goal} at t={t} but cannot hold it at t={bad_t}",
                cell=goal,
                conflict_time=bad_t,
                with_robot=other2,
            )
            record_prune(ev)
            cand = (0, bad_t, ev)
            if best_prune is None or (cand[0], -cand[1]) < (
                best_prune[0],
                -best_prune[1],
            ):
                best_prune = cand
            continue  # 也许早到不行、晚到反而行，继续搜索

        if t >= horizon:
            continue

        # wait + 4 邻域移动；wait 优先展开（倾向少动）
        candidates = [cell] + grid.neighbors(cell)
        for nxt in candidates:
            t_next = t + 1
            ev = _blocked_reason(constraints, cell, nxt, t_next, robot_id)
            if ev is not None:
                consider(nxt, t_next, ev)
                continue
            state = (nxt, t_next)
            if t_next < g_score.get(state, t_next + 1):
                g_score[state] = t_next
                came_from[state] = (cell, t)
                counter += 1
                heapq.heappush(
                    open_heap,
                    (t_next + manhattan(nxt, goal), counter, t_next, nxt),
                )

    detail = best_prune[2] if best_prune is not None else None
    message = (
        f"no conflict-free path from {start} to {goal} within horizon={horizon}"
    )
    if detail is not None:
        message += f"; closest blocking evidence: {detail['message']}"
    return PlanResult(
        False,
        evidence=_evidence(
            NO_PATH_IN_HORIZON,
            message,
            cell=detail.get("cell") if detail else None,
            conflict_time=detail.get("conflict_time") if detail else None,
            with_robot=detail.get("with_robot") if detail else None,
            frm=(tuple(detail["from"]) if detail and "from" in detail else None),
            to=(tuple(detail["to"]) if detail and "to" in detail else None),
            horizon=horizon,
            extra={
                "nodes_explored": nodes_explored,
                "prune_counts": prune_counts,
                "root_cause": detail["type"] if detail else None,
            },
        ),
        nodes_explored=nodes_explored,
        prune_counts=prune_counts,
    )


def revalidate_path(
    grid: StaticMap,
    path: list[Cell],
    constraints: Constraints,
    robot_id: int,
    window_end: int,
) -> dict[str, Any] | None:
    """提交前用最新地图 + 最新预订整条路径重新校验。

    依次检查：地图障碍、顶点冲突、终点停留、对向交换边冲突、终点停留至窗口末。
    返回首个冲突证据；全部通过返回 None。
    """
    if not path:
        return _evidence(NO_PATH_IN_HORIZON, "empty path")

    for t, cell in enumerate(path):
        if not grid.in_bounds(cell):
            return _evidence(OUT_OF_BOUNDS,
                             f"path cell {cell} out of bounds at t={t}",
                             cell=cell, conflict_time=t)
        if cell in grid.obstacles:
            return _evidence(
                MAP_OBSTACLE_ON_PATH,
                f"map changed: cell {cell} at t={t} is now an obstacle",
                cell=cell, conflict_time=t)
        other = constraints.vertex_robot(cell, t)
        if other is not None and other != robot_id:
            return _evidence(
                VERTEX_CONFLICT,
                f"cell {cell} occupied by robot {other} at t={t}",
                cell=cell, conflict_time=t, with_robot=other)
        ep = constraints.endpoint_holder(cell)
        if ep is not None and ep.robot_id != robot_id and ep.arrival <= t:
            return _evidence(
                ENDPOINT_OCCUPIED,
                f"cell {cell} permanently held by robot {ep.robot_id} at t={t}",
                cell=cell, conflict_time=t, with_robot=ep.robot_id)

    for t, (u, v) in enumerate(zip(path, path[1:])):
        if u == v:
            continue  # wait 不占边；对方抢入由顶点检查拦截
        edge = constraints.edge_conflict(u, v, t)
        if edge is not None:
            etype, erobot, eu, ev_cell = edge
            return _evidence(
                etype,
                f"edge conflict on {u}<->{v} during [{t},{t + 1}] "
                f"with robot {erobot}",
                conflict_time=t, with_robot=erobot, frm=u, to=v)

    arrival = len(path) - 1
    goal = path[-1]
    hold_to = max(window_end, arrival)
    for t in range(arrival + 1, hold_to + 1):
        other = constraints.vertex_robot(goal, t)
        if other is not None and other != robot_id:
            return _evidence(
                VERTEX_CONFLICT,
                f"goal {goal} occupied at t={t} while holding",
                cell=goal, conflict_time=t, with_robot=other)
        ep = constraints.endpoint_holder(goal)
        if ep is not None and ep.robot_id != robot_id and ep.arrival <= t:
            return _evidence(
                ENDPOINT_OCCUPIED,
                f"goal {goal} permanently held by robot {ep.robot_id} at t={t}",
                cell=goal, conflict_time=t, with_robot=ep.robot_id)
    return None
