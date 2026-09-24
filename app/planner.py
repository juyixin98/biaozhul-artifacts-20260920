"""空间-时间 A* 规划器（space-time A*）。

核心模型
========
* 地图是有限二维栅格 ``GridMap``，每个格子要么可通行要么是障碍；机器人只能
  "等待" 或移动到 4-邻接的可通行格子。
* 时间被离散成整数刻 ``t = 0,1,2,...``。状态是 ``(x, y, t)``。
* 已生效的预约给新规划施加两类约束（这也是多智能体路径规划里的标准区分）：

  - **顶点约束（vertex / node conflict）**：同一时刻 ``t`` 两个机器人占据同一格。
  - **边约束（edge / swap conflict）**：在同一对相邻时刻 ``t -> t+1``，两个机器人
    交换位置（相向而行）。只检查顶点冲突无法发现这种情况——本模块对两者都检查。

* 已有预约的机器人到达终点后会**无限等待**，因此其终点格在到达刻之后对别人是
  永久顶点占用（``permanent``），边约束不延续（等待不动不产生边交换）。

优先级
======
:func:`prioritized_batch` 按机器人 id 升序的固定顺序逐个规划（id 小者优先）。这是
**贪婪的、非完备**的：先规划的高优先级机器人不会为后来者让路，也不会回溯/重规划。
即使一组目标在联合解空间里可行，本算法也可能因为早期选择而给后来者判失败。
因此失败结果只表示"在该固定优先级顺序与已有预约下找不到路"，不证明全局无解。
"""

from __future__ import annotations

import heapq
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Set, Tuple

Cell = Tuple[int, int]

# 动作：(dx, dy)，含等待。WAIT 必须排第一，搜索在同等代价下优先等待。
WAIT: Cell = (0, 0)
NEIGHBOR_MOVES: Tuple[Cell, ...] = ((0, 0), (1, 0), (-1, 0), (0, 1), (0, -1))


class GridMap:
    """不可变栅格地图快照。

    ``width``/``height`` 为栅格数，坐标范围 ``0 <= x < width``、``0 <= y < height``。
    ``obstacles`` 为障碍格集合。
    """

    def __init__(self, width: int, height: int, obstacles: Iterable[Cell] = ()):
        if width <= 0 or height <= 0:
            raise ValueError("地图宽高必须为正整数")
        self.width = width
        self.height = height
        self.obstacles: Set[Cell] = {(int(x), int(y)) for x, y in obstacles}
        for x, y in self.obstacles:
            if not (0 <= x < width and 0 <= y < height):
                raise ValueError(f"障碍格 ({x},{y}) 超出地图范围")

    def in_bounds(self, cell: Cell) -> bool:
        x, y = cell
        return 0 <= x < self.width and 0 <= y < self.height

    def passable(self, cell: Cell) -> bool:
        return self.in_bounds(cell) and cell not in self.obstacles

    def neighbors_and_wait(self, cell: Cell) -> List[Cell]:
        """返回从 ``cell`` 出发一步可达的格子（含原地等待），仅含合法可通行格。"""
        x, y = cell
        out: List[Cell] = []
        for dx, dy in NEIGHBOR_MOVES:
            nxt = (x + dx, y + dy)
            if self.passable(nxt):
                out.append(nxt)
        return out

    def to_json(self) -> dict:
        return {
            "width": self.width,
            "height": self.height,
            "obstacles": sorted(list(self.obstacles)),
        }

    @classmethod
    def from_json(cls, data: dict) -> "GridMap":
        return cls(
            int(data["width"]),
            int(data["height"]),
            [(int(x), int(y)) for x, y in data.get("obstacles", [])],
        )


@dataclass
class VertexBlock:
    """顶点占用：在时刻 ``time`` 占据 ``cell``。"""

    cell: Cell
    time: int
    blocker_ref: str

    def __hash__(self) -> int:
        return hash((self.cell, self.time))


@dataclass
class EdgeBlock:
    """有向边占用：在刻 ``time`` 从 ``src`` 移动到 ``dst``（``time`` -> ``time+1``）。

    检查时按无向边处理，因此 ``(src->dst)`` 与别人的 ``(dst->src)`` 在同一刻冲突，
    即对向交换（swap）。
    """

    src: Cell
    dst: Cell
    time: int
    blocker_ref: str

    def __hash__(self) -> int:
        return hash((self.src, self.dst, self.time))


@dataclass
class PermanentBlock:
    """永久顶点占用：某个已预约机器人自 ``since_time`` 起永远停在 ``cell``。"""

    cell: Cell
    since_time: int
    blocker_ref: str

    def __hash__(self) -> int:
        return hash((self.cell, self.since_time))


@dataclass
class Constraints:
    """一组对新规划生效的时空约束。"""

    vertex: Set[VertexBlock] = field(default_factory=set)
    edges: Set[EdgeBlock] = field(default_factory=set)
    permanent: Set[PermanentBlock] = field(default_factory=set)

    def add(self, other: "Constraints") -> None:
        self.vertex |= other.vertex
        self.edges |= other.edges
        self.permanent |= other.permanent

    def vertex_blocker(self, cell: Cell, time: int) -> Optional[str]:
        for b in self.vertex:
            if b.cell == cell and b.time == time:
                return b.blocker_ref
        return None

    def edge_blocker(self, src: Cell, dst: Cell, time: int) -> Optional[str]:
        """无向边检查：同刻反向穿越同一条边即冲突。等待动作（src==dst）不参与。"""
        if src == dst:
            return None
        for b in self.edges:
            if b.time == time and b.src == dst and b.dst == src:
                return b.blocker_ref
        return None

    def permanent_blocker(self, cell: Cell, time: int) -> Optional[str]:
        """``time`` 刻 ``cell`` 是否落在某个永久终点占用上。"""
        for b in self.permanent:
            if b.cell == cell and time >= b.since_time:
                return b.blocker_ref
        return None


@dataclass
class SearchEvidence:
    """失败时收集的冲突证据。

    * ``kind``：``start_blocked``（起点即冲突）/ ``permanent_goal``（终点被永久占用）/
      ``exhausted``（在 horizon/node 上限内找不到可行路）。
    * ``blockers``：搜索中真正遇到的阻挡（含边交换阻挡，证明没有只查顶点）。
    """

    kind: str
    detail: str
    blockers: List[dict] = field(default_factory=list)

    def to_json(self) -> dict:
        return {"kind": self.kind, "detail": self.detail, "blockers": self.blockers}


@dataclass
class SearchResult:
    found: bool
    path: List[Cell]
    evidence: Optional[SearchEvidence]
    expanded_nodes: int
    horizon: int


def _manhattan(a: Cell, b: Cell) -> int:
    return abs(a[0] - b[0]) + abs(a[1] - b[1])


def plan_one(
    grid: GridMap,
    start: Cell,
    goal: Cell,
    constraints: Constraints,
    *,
    start_time: int = 0,
    horizon: int = 200,
    max_nodes: int = 200_000,
    enforce_edges: bool = True,
    actor_ref: str = "robot",
) -> SearchResult:
    """为单个机器人生成从 ``start``（刻 ``start_time``）到 ``goal`` 的无冲突路径。

    路径 ``path[k]`` 是机器人在刻 ``start_time + k`` 占据的格子，``path[0] == start``，
    末项为 ``goal``，相邻项相同（等待）或 4-邻接。

    ``enforce_edges=False`` 时关闭边交换检查——仅用于测试，以证明关闭边检查会漏掉
    对向交换；生产路径永远用默认值 ``True``。
    """
    if not grid.passable(start):
        return SearchResult(
            False,
            [],
            SearchEvidence("start_blocked", f"起点 {start} 不可通行或越界"),
            0,
            horizon,
        )
    if not grid.passable(goal):
        return SearchResult(
            False,
            [],
            SearchEvidence("permanent_goal", f"终点 {goal} 是障碍或越界"),
            0,
            horizon,
        )
    if start_time < 0:
        raise ValueError("start_time 不能为负")

    # 起点在出发刻的占用检查（不含永久 since>start_time 的条目，但永久占用必然
    # 从某个更早/相等的刻起存在；这里统一调用现有检查）。
    vb = constraints.vertex_blocker(start, start_time)
    pb = constraints.permanent_blocker(start, start_time)
    blocker = vb or pb
    if blocker is not None:
        return SearchResult(
            False,
            [],
            SearchEvidence(
                "start_blocked",
                f"起点 {start} 在刻 {start_time} 已被 {blocker} 占用",
                [
                    {
                        "type": "vertex",
                        "cell": list(start),
                        "time": start_time,
                        "blocker_ref": blocker,
                    }
                ],
            ),
            0,
            horizon,
        )

    # 终点若被任何永久终点占用，则永远不可能作为最终停留点。
    goal_perm = next((b for b in constraints.permanent if b.cell == goal), None)
    if goal_perm is not None:
        return SearchResult(
            False,
            [],
            SearchEvidence(
                "permanent_goal",
                f"终点 {goal} 被 {goal_perm.blocker_ref} 自刻 {goal_perm.since_time} 起永久占用",
                [
                    {
                        "type": "permanent",
                        "cell": list(goal),
                        "since_time": goal_perm.since_time,
                        "blocker_ref": goal_perm.blocker_ref,
                    }
                ],
            ),
            0,
            horizon,
        )

    # (f, tie, (x,y,t)) —— tie 用插入序号打破并列。
    counter = 0
    open_heap: List[Tuple[int, int, Tuple[int, int, int]]] = []
    came_from: Dict[Tuple[int, int, int], Optional[Tuple[int, int, int]]] = {}
    g_score: Dict[Tuple[int, int, int], int] = {}
    start_state = (start[0], start[1], start_time)
    g_score[start_state] = 0
    heapq.heappush(open_heap, (_manhattan(start, goal), counter, start_state))
    came_from[start_state] = None

    encountered: Dict[str, dict] = {}
    expanded = 0
    goal_state: Optional[Tuple[int, int, int]] = None

    def remember(b: dict) -> None:
        # 去重：同一 blocker_ref/type/时间只记一条证据。
        key = repr(b)
        encountered.setdefault(key, b)

    while open_heap:
        _, _, state = heapq.heappop(open_heap)
        x, y, t = state
        g = g_score[state]
        if expanded >= max_nodes:
            break
        expanded += 1

        cell = (x, y)
        if cell == goal:
            # 成为终点还要保证在此停留合法：goal 刻本身已在扩展时可用；
            # 永久占用的情况已在上面 goal_perm 排除。
            goal_state = state
            break

        if t - start_time >= horizon:
            continue

        for nxt in grid.neighbors_and_wait(cell):
            nt = t + 1
            nstate = (nxt[0], nxt[1], nt)

            vb = constraints.vertex_blocker(nxt, nt)
            if vb is not None:
                remember(
                    {"type": "vertex", "cell": list(nxt), "time": nt, "blocker_ref": vb}
                )
                continue
            pb = constraints.permanent_blocker(nxt, nt)
            if pb is not None:
                remember(
                    {
                        "type": "permanent",
                        "cell": list(nxt),
                        "time": nt,
                        "blocker_ref": pb,
                    }
                )
                continue
            if enforce_edges:
                eb = constraints.edge_blocker(cell, nxt, t)
                if eb is not None:
                    remember(
                        {
                            "type": "edge",
                            "src": list(cell),
                            "dst": list(nxt),
                            "time": t,
                            "blocker_ref": eb,
                        }
                    )
                    continue

            ng = g + 1
            if nstate not in g_score or ng < g_score[nstate]:
                g_score[nstate] = ng
                came_from[nstate] = state
                f = ng + _manhattan(nxt, goal)
                counter += 1
                heapq.heappush(open_heap, (f, counter, nstate))

    if goal_state is not None:
        # 回溯。
        rev: List[Cell] = []
        cur: Optional[Tuple[int, int, int]] = goal_state
        while cur is not None:
            rev.append((cur[0], cur[1]))
            cur = came_from[cur]
        rev.reverse()
        return SearchResult(True, rev, None, expanded, horizon)

    if expanded >= max_nodes:
        kind, detail = (
            "exhausted",
            f"扩展节点数达到上限 {max_nodes}，未能找到 {actor_ref} 到 {goal} 的无冲突路径",
        )
    else:
        kind, detail = (
            "exhausted",
            f"在时间窗 {horizon} 内不存在 {actor_ref} 从 {start} 到 {goal} 的无冲突路径"
            "（固定优先级、无绕行时间窗或被永久终点/边交换封死）",
        )
    return SearchResult(
        False,
        [],
        SearchEvidence(kind, detail, list(encountered.values())),
        expanded,
        horizon,
    )


@dataclass
class Planned:
    """单个机器人规划成功的结果。"""

    robot_id: str
    path: List[Cell]
    arrival_time: int


@dataclass
class BatchFailure:
    """批次中某个机器人规划失败的证据。"""

    robot_id: str
    start: Cell
    goal: Cell
    evidence: SearchEvidence

    def to_json(self) -> dict:
        return {
            "robot_id": self.robot_id,
            "start": list(self.start),
            "goal": list(self.goal),
            "evidence": self.evidence.to_json(),
        }


def constraints_from_plans(plans: Iterable[Tuple[str, List[Cell]]], start_time: int) -> Constraints:
    """把已经规划好的路径（同一批次里高优先级者）转成约束。

    末格按"无限等待"处理：加入 :class:`PermanentBlock`，但**不**再加边约束。
    """
    cons = Constraints()
    for robot_id, path in plans:
        if not path:
            continue
        for k, cell in enumerate(path):
            cons.vertex.add(VertexBlock(cell, start_time + k, robot_id))
        for k in range(len(path) - 1):
            src, dst = path[k], path[k + 1]
            if src != dst:
                cons.edges.add(EdgeBlock(src, dst, start_time + k, robot_id))
        last = path[-1]
        cons.permanent.add(PermanentBlock(last, start_time + len(path) - 1, robot_id))
    return cons


def prioritized_batch(
    grid: GridMap,
    requests: List[Tuple[str, Cell, Cell]],
    base: Constraints,
    *,
    start_time: int = 0,
    horizon: int = 200,
    max_nodes: int = 200_000,
    enforce_edges: bool = True,
) -> Tuple[Optional[List[Planned]], Optional[BatchFailure]]:
    """固定优先级（按 ``requests`` 给定顺序）的贪婪批量规划。

    成功返回 ``(plans, None)``；任一机器人失败返回 ``(None, failure)``。已经成功
    规划出的路径**不会提交**——由调用方决定（本服务的批量接口是原子的：要么全成功
    要么一个也不写入）。
    """
    planned: List[Planned] = []
    cons = Constraints()
    cons.add(base)
    for robot_id, start, goal in requests:
        res = plan_one(
            grid,
            start,
            goal,
            cons,
            start_time=start_time,
            horizon=horizon,
            max_nodes=max_nodes,
            enforce_edges=enforce_edges,
            actor_ref=robot_id,
        )
        if not res.found:
            return None, BatchFailure(robot_id, start, goal, res.evidence)  # type: ignore[arg-type]
        planned.append(Planned(robot_id, res.path, start_time + len(res.path) - 1))
        cons.add(constraints_from_plans([(robot_id, res.path)], start_time))
    return planned, None


def validate_path(
    grid: GridMap,
    start: Cell,
    path: List[Cell],
    constraints: Constraints,
    *,
    start_time: int = 0,
    enforce_edges: bool = True,
) -> Optional[dict]:
    """独立复核一条已生成路径的合法性（防御性二次校验）。

    返回 ``None`` 表示合法；否则返回第一个冲突的描述字典。覆盖：边界/障碍、起点一致、
    单步动作（等待或 4-邻接）、每刻顶点占用、永久终点占用、同刻边交换。
    """
    if not path:
        return {"type": "empty_path"}
    if path[0] != tuple(start):
        return {
            "type": "start_mismatch",
            "expected": list(start),
            "actual": list(path[0]),
        }
    for k, cell in enumerate(path):
        cell = (cell[0], cell[1])
        if not grid.passable(cell):
            return {"type": "illegal_cell", "cell": list(cell), "time": start_time + k}
        t = start_time + k
        vb = constraints.vertex_blocker(cell, t)
        if vb is not None:
            return {"type": "vertex", "cell": list(cell), "time": t, "blocker_ref": vb}
        pb = constraints.permanent_blocker(cell, t)
        if pb is not None:
            return {"type": "permanent", "cell": list(cell), "time": t, "blocker_ref": pb}
        if k > 0:
            prev = (path[k - 1][0], path[k - 1][1])
            if cell != prev and _manhattan(prev, cell) != 1:
                return {
                    "type": "bad_move",
                    "src": list(prev),
                    "dst": list(cell),
                    "time": t - 1,
                }
            if enforce_edges:
                eb = constraints.edge_blocker(prev, cell, t - 1)
                if eb is not None:
                    return {
                        "type": "edge",
                        "src": list(prev),
                        "dst": list(cell),
                        "time": t - 1,
                        "blocker_ref": eb,
                    }
    return None
