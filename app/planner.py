"""D* Lite 增量路径搜索 (Koenig & Likhachev, 2002 规范实现) 与独立 Dijkstra 基准。

约定:
- 有向视角下 ``succ(s)`` 是从 s 可一步到达的单元, ``pred(s)`` 是可一步到达 s 的单元;
  栅格边对称, 二者均为"当前可通行"的几何邻接单元。
- 边代价对称: 直边 = 两端权重均值, 斜边 = sqrt(2) * 两端权重均值。
- 障碍单元不参与搜索(不入队、不提供边)。
- 八邻接禁止斜穿两个障碍单元的夹角 (corner cutting)。
- 启发式 h = w_free_min * octile, 一致且可纳; 若地图修改使自由单元最小权重下降,
  增量不变量失效, 此时执行完整重置 (真实的正确性保护, 不是静默沿用旧搜索)。
"""

from __future__ import annotations

import heapq
import math
from dataclasses import dataclass

import numpy as np

from .grid import CONNECTIVITY_4, CONNECTIVITY_8, SQRT2

INF = float("inf")
_COST_EPS = 1e-9
# 键值比较容差: km 累加与边代价求和会引入 ~1e-15 舍入误差, 可能使本应严格
# 按 k1 平局->k2 排序的键序翻转, 导致主循环在受影响节点尚未处理时提前终止。
# 比较键时把 1e-10 以内的 k1 差视为平局, 回退到 k2 比较。
_KEY_EPS = 1e-10


def _key_lt(a: tuple[float, float], b: tuple[float, float]) -> bool:
    if a[0] < b[0] - _KEY_EPS:
        return True
    if abs(a[0] - b[0]) <= _KEY_EPS and a[1] < b[1] - _KEY_EPS:
        return True
    return False


def _key_gt(a: tuple[float, float], b: tuple[float, float]) -> bool:
    return _key_lt(b, a)

# 8 邻域: 4 直 + 4 斜
_NEIGH8 = (
    (-1, 0),
    (1, 0),
    (0, -1),
    (0, 1),
    (-1, -1),
    (-1, 1),
    (1, -1),
    (1, 1),
)
_NEIGH4 = ((-1, 0), (1, 0), (0, -1), (0, 1))


class GridView:
    """对 (weights, blocked) 栅格的只读代价视图。"""

    __slots__ = ("weights", "blocked", "rows", "cols", "connectivity")

    def __init__(self, weights: np.ndarray, blocked: np.ndarray, connectivity: int):
        if connectivity not in (CONNECTIVITY_4, CONNECTIVITY_8):
            raise ValueError("connectivity 只能是 4 或 8")
        self.weights = weights
        self.blocked = blocked
        self.rows, self.cols = weights.shape
        self.connectivity = connectivity

    def in_bounds(self, r: int, c: int) -> bool:
        return 0 <= r < self.rows and 0 <= c < self.cols

    def is_blocked(self, r: int, c: int) -> bool:
        return bool(self.blocked[r, c])

    def edge_cost(self, a: tuple[int, int], b: tuple[int, int]) -> float | None:
        """返回 a->b 的穿越代价; 不可穿越返回 None。"""
        ra, ca = a
        rb, cb = b
        if not (self.in_bounds(ra, ca) and self.in_bounds(rb, cb)):
            return None
        if self.blocked[ra, ca] or self.blocked[rb, cb]:
            return None
        dr, dc = rb - ra, cb - ca
        if (dr, dc) not in _NEIGH8:
            return None  # 仅允许邻接步
        if dr != 0 and dc != 0:
            if self.connectivity == CONNECTIVITY_4:
                return None
            # 斜穿夹角禁止: 两个正交中间单元都被堵时不能斜走
            if self.blocked[ra, cb] and self.blocked[rb, ca]:
                return None
            return SQRT2 * (float(self.weights[ra, ca]) + float(self.weights[rb, cb])) * 0.5
        return (float(self.weights[ra, ca]) + float(self.weights[rb, cb])) * 0.5

    def geometric_neighbors(self, r: int, c: int):
        """全部几何邻居(不考虑障碍), 用于受影响集合扩张。"""
        moves = _NEIGH8 if self.connectivity == CONNECTIVITY_8 else _NEIGH4
        for dr, dc in moves:
            nr, nc = r + dr, c + dc
            if self.in_bounds(nr, nc):
                yield nr, nc

    def passable_neighbors(self, r: int, c: int):
        """当前可一步通行的邻居及边代价。"""
        moves = _NEIGH8 if self.connectivity == CONNECTIVITY_8 else _NEIGH4
        for dr, dc in moves:
            nr, nc = r + dr, c + dc
            if not self.in_bounds(nr, nc):
                continue
            cost = self.edge_cost((r, c), (nr, nc))
            if cost is not None:
                yield nr, nc, cost


@dataclass
class SearchDiagnostics:
    """一次规划的诊断量(观测值, 非性能承诺)。"""

    heap_pops: int = 0
    expanded_nonstale: int = 0
    stale_key_reinserts: int = 0
    rhs_recomputations: int = 0
    full_reset: bool = False
    reason: str = ""

    def to_dict(self) -> dict:
        return {
            "heap_pops": self.heap_pops,
            "expanded_nonstale": self.expanded_nonstale,
            "stale_key_reinserts": self.stale_key_reinserts,
            "rhs_recomputations": self.rhs_recomputations,
            "full_reset": self.full_reset,
            "reason": self.reason,
        }


class DStarLite:
    """D* Lite 增量规划器(反向搜索: 目标固定, 起点可移动)。"""

    def __init__(self, view: GridView, start: tuple[int, int], goal: tuple[int, int]):
        self.view = view
        if not view.in_bounds(*start):
            raise PlannerError(f"起点越界: {start}")
        if not view.in_bounds(*goal):
            raise PlannerError(f"目标越界: {goal}")
        if view.is_blocked(*start):
            raise PlannerError(f"起点位于障碍单元: {start}")
        if view.is_blocked(*goal):
            raise PlannerError(f"目标位于障碍单元: {goal}")
        self.start = start
        self.goal = goal
        self.km = 0.0
        init_diag = SearchDiagnostics(full_reset=True, reason="初始化")
        self._reset_search(full=True, reason="初始化", diag=init_diag)
        self.compute_shortest_path(init_diag)

    # ------------------------------------------------------------------ #
    # 初始化 / 重置
    # ------------------------------------------------------------------ #
    def _new_state_maps(self):
        n = self.view.rows * self.view.cols
        return np.full(n, INF, dtype=np.float64), np.full(n, INF, dtype=np.float64)

    def _reset_search(self, full: bool, reason: str, diag: SearchDiagnostics | None = None):
        self.g, self.rhs = self._new_state_maps()
        self._open: list = []
        self._ver: dict[int, int] = {}
        self.km = 0.0
        # 启发式尺度: 自由单元最小权重(在当前权重不变小时保持可纳)
        free_mask = ~self.view.blocked
        if not free_mask.any():
            raise PlannerError("不存在任何自由单元")
        self.hscale = float(self.view.weights[free_mask].min())
        gi = self._idx(*self.goal)
        self.rhs[gi] = 0.0
        self._push(gi)
        self.last_diag = diag if diag is not None else SearchDiagnostics(
            full_reset=full, reason=reason
        )

    def _idx(self, r: int, c: int) -> int:
        return r * self.view.cols + c

    def _cell(self, idx: int) -> tuple[int, int]:
        return divmod(idx, self.view.cols)

    # ------------------------------------------------------------------ #
    # 键值与优先队列(惰性删除, 版本号判定陈旧)
    # ------------------------------------------------------------------ #
    def _h(self, a: tuple[int, int], b: tuple[int, int]) -> float:
        dr = abs(a[0] - b[0])
        dc = abs(a[1] - b[1])
        # octile 距离
        d = (max(dr, dc) - min(dr, dc)) + SQRT2 * min(dr, dc)
        return self.hscale * d

    def _key(self, idx: int) -> tuple[float, float]:
        gv, rv = self.g[idx], self.rhs[idx]
        k2 = min(gv, rv)
        k1 = k2 + self._h(self._cell(idx), self.start) + self.km
        return (k1, k2)

    def _push(self, idx: int):
        v = self._ver.get(idx, 0) + 1
        self._ver[idx] = v
        heapq.heappush(self._open, (self._key(idx), v, idx))

    def _invalidate(self, idx: int):
        """从开放集逻辑移除(版本号失效)。"""
        self._ver[idx] = self._ver.get(idx, 0) + 1

    def _refresh(self, idx: int):
        """按规范重算 rhs(u) 并维护开放集成员关系。"""
        self.last_diag.rhs_recomputations += 1
        r, c = self._cell(idx)
        if (r, c) == self.goal:
            self.rhs[idx] = 0.0
        elif self.view.is_blocked(r, c):
            return  # 障碍单元不持有搜索状态
        else:
            best = INF
            for nr, ncr, cost in self.view.passable_neighbors(r, c):
                val = cost + self.g[self._idx(nr, ncr)]
                if val < best:
                    best = val
            self.rhs[idx] = best
        self._invalidate(idx)
        if self.g[idx] != self.rhs[idx]:
            self._push(idx)

    # ------------------------------------------------------------------ #
    # 主搜索过程 (ComputeShortestPath, 带旧键重插的规范形式)
    # ------------------------------------------------------------------ #
    def compute_shortest_path(self, diag: SearchDiagnostics | None = None) -> SearchDiagnostics:
        if diag is None:
            diag = SearchDiagnostics(reason="compute")
        self.last_diag = diag
        start_i = self._idx(*self.start)

        while self._open:
            diag.heap_pops += 1
            k_old, ver, u = heapq.heappop(self._open)
            if ver != self._ver.get(u, -1):
                continue  # 已失效的陈旧条目

            k_start = self._key(start_i)
            terminates = not (
                _key_lt(k_old, k_start)
                or self.rhs[start_i] != self.g[start_i]
            )
            if terminates:
                # 未消费, 放回
                heapq.heappush(self._open, (k_old, ver, u))
                break

            k_cur = self._key(u)
            if _key_gt(k_old, k_cur):
                # 键已过期: 用新键重插
                v = self._ver.get(u, 0) + 1
                self._ver[u] = v
                heapq.heappush(self._open, (k_cur, v, u))
                diag.stale_key_reinserts += 1
                continue

            diag.expanded_nonstale += 1
            ur, uc = self._cell(u)
            if self.g[u] > self.rhs[u]:
                # 过一致
                self.g[u] = self.rhs[u]
                self._invalidate(u)
                for sr, sc, _ in self.view.passable_neighbors(ur, uc):
                    self._refresh(self._idx(sr, sc))
            else:
                # 欠一致
                self.g[u] = INF
                self._invalidate(u)
                seen = {u}
                self._refresh(u)
                for sr, sc, _ in self.view.passable_neighbors(ur, uc):
                    si = self._idx(sr, sc)
                    if si not in seen:
                        seen.add(si)
                        self._refresh(si)
        return diag

    # ------------------------------------------------------------------ #
    # 增量事件
    # ------------------------------------------------------------------ #
    def move_start(self, new_start: tuple[int, int]) -> SearchDiagnostics:
        """起点移动: 累计 km 偏移后增量修复。"""
        if new_start == self.start:
            return self.compute_shortest_path()
        nr, nc = new_start
        if not self.view.in_bounds(nr, nc):
            raise PlannerError(f"起点越界: {new_start}")
        if self.view.is_blocked(nr, nc):
            raise PlannerError(f"起点位于障碍单元: {new_start}")
        self.km += self._h(self.start, new_start)
        self.start = new_start
        return self.compute_shortest_path()

    def update_grid(
        self,
        view: GridView,
        changed_cells: list[tuple[int, int]],
    ) -> SearchDiagnostics:
        """绑定新地图视图并增量修复。

        ``changed_cells`` 是障碍/权重发生变化的单元; 受影响集合扩张到其全部几何邻居,
        与规范 Main() 中"更新所有受影响边端点"等价。若自由单元最小权重下降导致
        启发式不可纳, 则完整重置(绝不沿用失效旧状态)。
        """
        self.view = view
        free_mask = ~view.blocked
        if not free_mask.any():
            raise PlannerError("地图上不存在自由单元")
        new_floor = float(view.weights[free_mask].min())

        if new_floor + _COST_EPS < self.hscale:
            # 自由单元最小权重下降: 原启发式相对新代价不再可纳, 增量键序不变量
            # 失效。完整重置后重新搜索, 绝不沿用旧地图下的搜索状态。
            self.hscale = new_floor
            diag = SearchDiagnostics(
                full_reset=True, reason="自由单元最小权重下降, 启发式失效"
            )
            self._reset_search(full=True, reason=diag.reason, diag=diag)
            return self.compute_shortest_path(diag)
        if new_floor > self.hscale + _COST_EPS:
            # 地板上升: 旧启发式仍然可纳(更保守), 只需更新尺度, 搜索可增量继续
            self.hscale = new_floor

        self.last_diag = SearchDiagnostics(reason="incremental_map_update")
        affected: set[int] = set()
        for r, c in changed_cells:
            affected.add(self._idx(r, c))
            for nr, nc in view.geometric_neighbors(r, c):
                affected.add(self._idx(nr, nc))
        for idx in affected:
            r, c = self._cell(idx)
            if view.is_blocked(r, c):
                continue  # 障碍不持有状态; 它的可通行邻居已在受影响集合中
            self._refresh(idx)
        return self.compute_shortest_path()

    # ------------------------------------------------------------------ #
    # 结果
    # ------------------------------------------------------------------ #
    def optimal_cost(self) -> float:
        return self.g[self._idx(*self.start)]

    def extract_path(self) -> list[tuple[int, int]] | None:
        """在最优边子图上以 BFS 提取一条起点->目标路径; 不可达返回 None。

        边 (u,v) 属于最优子结构当且仅当
        ``g[u] == cost(u,v) + g[v]`` (容差判定)。
        D* Lite 在零代价高原上可能于 g(start)==rhs(start) 时即终止,
        高原内部单元的 g 未必全部落定, 所以这里不用"g 必须严格下降"的贪心,
        而用 BFS + visited 处理 g[u]==g[v] 的零代价边。
        BFS 在最优边子图中找到目标即保证路径总成本等于 g[start]。
        """
        import collections

        start_i = self._idx(*self.start)
        goal_i = self._idx(*self.goal)
        if math.isinf(self.g[start_i]):
            return None
        prev: dict[int, int] = {}
        seen = {start_i}
        queue = collections.deque([start_i])
        found = False
        while queue:
            u = queue.popleft()
            if u == goal_i:
                found = True
                break
            ur, uc = self._cell(u)
            gu = self.g[u]
            candidates: list[tuple[float, int]] = []
            for nr, nc, cost in self.view.passable_neighbors(ur, uc):
                # passable_neighbors 已经用 edge_cost 真实校验:
                # 越界/障碍/八邻接夹角斜穿 均被排除
                ni = self._idx(nr, nc)
                gv = self.g[ni]
                if ni in seen or math.isinf(gv):
                    continue
                if abs(gu - (gv + cost)) <= 1e-7:
                    candidates.append((gv, ni))
            # g 更小的最优边优先, 其余按编号确定序; 全部入队(BFS 覆盖高原)
            candidates.sort(key=lambda t: (t[0], t[1]))
            for _, ni in candidates:
                if ni in seen:
                    continue
                seen.add(ni)
                prev[ni] = u
                queue.append(ni)
        if not found:
            return None
        chain = [goal_i]
        while chain[-1] != start_i:
            chain.append(prev[chain[-1]])
        chain.reverse()
        return [self._cell(i) for i in chain]


class PlannerError(Exception):
    """规划参数错误(起点/目标在障碍上、越界等)。"""


# ====================================================================== #
# 独立 Dijkstra 基准: 从起点正向搜索, 与 D* Lite 不共享任何搜索状态
# ====================================================================== #
def dijkstra(
    view: GridView,
    start: tuple[int, int],
    goal: tuple[int, int],
) -> dict:
    """返回 {cost, path, expansions}。cost=inf 表示不可达。"""
    cols = view.cols
    if not view.in_bounds(*start) or view.is_blocked(*start):
        raise PlannerError(f"起点不可用: {start}")
    if not view.in_bounds(*goal) or view.is_blocked(*goal):
        raise PlannerError(f"目标不可用: {goal}")

    def idx(r, c):
        return r * cols + c

    dist = {idx(*start): 0.0}
    prev: dict[int, int] = {}
    open_heap: list[tuple[float, int]] = [(0.0, idx(*start))]
    expansions = 0
    goal_i = idx(*goal)

    while open_heap:
        d, u = heapq.heappop(open_heap)
        if d != dist.get(u):
            continue
        expansions += 1
        if u == goal_i:
            break
        ur, uc = divmod(u, cols)
        for nr, nc, cost in view.passable_neighbors(ur, uc):
            ni = idx(nr, nc)
            nd = d + cost
            if nd < dist.get(ni, INF) - _COST_EPS:
                dist[ni] = nd
                prev[ni] = u
                heapq.heappush(open_heap, (nd, ni))

    best = dist.get(goal_i, INF)
    if math.isinf(best):
        return {"cost": INF, "path": None, "expansions": expansions}

    path_idx = [goal_i]
    while path_idx[-1] != idx(*start):
        path_idx.append(prev[path_idx[-1]])
    path_idx.reverse()
    path = [divmod(i, cols) for i in path_idx]
    # 独立累加路径代价, 不信任 dist
    total = 0.0
    for a, b in zip(path, path[1:]):
        total += view.edge_cost(a, b)  # type: ignore[operator]
    return {"cost": total, "path": path, "expansions": expansions}
