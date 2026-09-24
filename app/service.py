"""服务层: 地图与规划器的内存注册表, 快照绑定与最优性校验。"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

import numpy as np

from .grid import (
    MapStore,
    Snapshot,
    SnapshotNotFound,
    StaleSnapshot,
)
from .planner import (
    DStarLite,
    GridView,
    PlannerError,
    dijkstra,
)

COST_TOL = 1e-7
PATH_COST_TOL = 1e-6


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _new_id() -> str:
    return uuid.uuid4().hex


class ApiError(Exception):
    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


class PlannerRecord:
    def __init__(
        self,
        planner_id: str,
        map_id: str,
        store: MapStore,
        snap: Snapshot,
        start: tuple[int, int],
        goal: tuple[int, int],
        planner: DStarLite,
    ):
        self.planner_id = planner_id
        self.map_id = map_id
        self.store = store
        self.snap = snap
        self.start = start
        self.goal = goal
        self.planner = planner
        self.seq = 0

    @property
    def view(self) -> GridView:
        return self.planner.view


class PlannerService:
    def __init__(self):
        self.maps: dict[str, MapStore] = {}
        self.planners: dict[str, PlannerRecord] = {}

    # ------------------------------------------------------------------ #
    def create_map(
        self,
        rows: int,
        cols: int,
        weights: list[list[float]] | None,
        obstacles: list[list[int]] | None,
        connectivity: int,
    ) -> tuple[str, Snapshot]:
        try:
            store = MapStore(rows, cols, connectivity)
        except ValueError as e:
            raise ApiError(400, "invalid_map", str(e))

        w = np.ones((rows, cols), dtype=np.float64)
        if weights is not None:
            arr = np.asarray(weights, dtype=np.float64)
            if arr.shape != (rows, cols):
                raise ApiError(
                    400, "invalid_weights", f"weights 形状必须为 {rows}x{cols}"
                )
            if not np.all(np.isfinite(arr)) or np.any(arr < 0):
                raise ApiError(400, "negative_weight", "权重必须全部为非负有限值")
            w = arr

        b = np.zeros((rows, cols), dtype=bool)
        for i, (r, c) in enumerate(obstacles or []):
            if not (0 <= r < rows and 0 <= c < cols):
                raise ApiError(400, "invalid_obstacle", f"障碍坐标越界: ({r},{c})")
            if b[r, c]:
                raise ApiError(400, "duplicate_obstacle", f"障碍重复: ({r},{c})")
            b[r, c] = True

        map_id = _new_id()
        self.maps[map_id] = store
        snap = store.commit_genesis(w, b, _now())
        return map_id, snap

    def _get_map(self, map_id: str) -> MapStore:
        store = self.maps.get(map_id)
        if store is None:
            raise ApiError(404, "map_not_found", f"地图不存在: {map_id}")
        return store

    def get_map_info(self, map_id: str) -> dict:
        store = self._get_map(map_id)
        return {
            "map_id": map_id,
            "rows": store.rows,
            "cols": store.cols,
            "connectivity": store.connectivity,
            "head": store.head.to_info(),
            "versions": [s.to_info() for s in store.snapshots],
        }

    def update_map(
        self,
        map_id: str,
        changes: list[dict],
        expected_snapshot: str | None,
        freeze_cells: list[tuple[int, int]] | None = None,
    ) -> Snapshot:
        store = self._get_map(map_id)
        base = store.head
        if expected_snapshot is not None and expected_snapshot != base.snapshot_id:
            raise ApiError(
                409,
                "snapshot_conflict",
                f"expected_snapshot={expected_snapshot[:12]} 与当前链头 "
                f"{base.snapshot_id[:12]} 不一致",
            )
        try:
            snap = store.apply_changes(base, changes, _now())
        except StaleSnapshot as e:
            raise ApiError(409, "snapshot_conflict", str(e))
        except ValueError as e:
            raise ApiError(400, "invalid_change", str(e))
        for cell in freeze_cells or []:
            if snap.blocked[cell]:
                # 回滚链头(该快照作废)——保持规划端点始终可穿越
                store.snapshots.pop()
                raise ApiError(
                    409,
                    "endpoint_blocked",
                    f"变更会把端点单元 {cell} 变为障碍, 已拒绝",
                )
        return snap

    def verify_map(self, map_id: str) -> dict:
        store = self._get_map(map_id)
        return store.verify_chain()

    # ------------------------------------------------------------------ #
    def _check_endpoint(
        self, view: GridView, point: tuple[int, int], name: str
    ) -> None:
        if not view.in_bounds(*point):
            raise ApiError(400, f"invalid_{name}", f"{name} 坐标越界: {point}")
        if view.is_blocked(*point):
            raise ApiError(422, f"{name}_blocked", f"{name} 位于障碍单元: {point}")

    def _make_view(self, snap: Snapshot, store: MapStore) -> GridView:
        return GridView(snap.weights, snap.blocked, store.connectivity)

    def create_planner(
        self,
        map_id: str,
        start: tuple[int, int],
        goal: tuple[int, int],
        snapshot_id: str | None = None,
    ) -> PlannerRecord:
        store = self._get_map(map_id)
        try:
            snap = store.head if snapshot_id is None else store.require(snapshot_id)
        except SnapshotNotFound:
            raise ApiError(404, "snapshot_not_found", f"快照不存在: {snapshot_id}")
        view = self._make_view(snap, store)
        self._check_endpoint(view, start, "start")
        self._check_endpoint(view, goal, "goal")
        try:
            planner = DStarLite(view, start, goal)
        except PlannerError as e:
            raise ApiError(422, "planner_error", str(e))
        rec = PlannerRecord(_new_id(), map_id, store, snap, start, goal, planner)
        self.planners[rec.planner_id] = rec
        return rec

    def _get_planner(self, planner_id: str) -> PlannerRecord:
        rec = self.planners.get(planner_id)
        if rec is None:
            raise ApiError(404, "planner_not_found", f"规划器不存在: {planner_id}")
        return rec

    def get_planner_info(self, planner_id: str) -> dict:
        rec = self._get_planner(planner_id)
        return {
            "planner_id": planner_id,
            "map_id": rec.map_id,
            "start": list(rec.start),
            "goal": list(rec.goal),
            "snapshot": rec.snap.to_info(),
            "seq": rec.seq,
        }

    # ------------------------------------------------------------------ #
    def _plan_result(self, rec: PlannerRecord, diag, map_changed: bool) -> dict:
        rec.seq += 1
        view = rec.view
        cost = rec.planner.optimal_cost()
        path = rec.planner.extract_path()

        # 独立 Dijkstra 基准(每次都重算, 不共享任何增量状态)
        bench = dijkstra(view, rec.start, rec.goal)
        bench_inf = bool(np.isinf(bench["cost"]))
        cost_inf = bool(np.isinf(cost))
        if cost_inf:
            optimal_match = bench_inf
        else:
            optimal_match = bool(abs(cost - bench["cost"]) <= COST_TOL)

        path_ok = False
        path_cost = None
        if path is not None:
            # 独立校验: 邻接、障碍、夹角斜穿、以及路径真实代价
            total = 0.0
            valid = (
                path[0] == rec.start
                and path[-1] == rec.goal
                and not view.is_blocked(*path[0])
                and not view.is_blocked(*path[-1])
            )
            for a, b in zip(path, path[1:]):
                e = view.edge_cost(a, b)
                if e is None:
                    valid = False
                    break
                total += e
            if valid:
                path_ok = bool(abs(total - bench["cost"]) <= PATH_COST_TOL)
                path_cost = total
        else:
            path_ok = bench_inf

        if not optimal_match:
            # 真实失败如实报告: 绝不伪装成成功
            return {
                "seq": rec.seq,
                "ok": False,
                "error_code": "optimality_mismatch",
                "message": (
                    f"D*Lite 成本 {cost} 与独立 Dijkstra 最优成本 "
                    f"{bench['cost']} 不一致"
                ),
                "snapshot_id": rec.snap.snapshot_id,
                "map_changed": map_changed,
                "diagnostics": diag.to_dict(),
                "benchmark": {
                    "dijkstra_cost": _fin(bench["cost"]),
                    "dijkstra_expansions": bench["expansions"],
                },
            }

        return {
            "seq": rec.seq,
            "ok": True,
            "reachable": not cost_inf,
            "cost": _fin(cost),
            "path": [list(p) for p in path] if path is not None else None,
            "path_cost": _fin(path_cost) if path_cost is not None else None,
            "path_valid": path_ok,
            "snapshot_id": rec.snap.snapshot_id,
            "snapshot_version": rec.snap.version,
            "map_changed": map_changed,
            "diagnostics": diag.to_dict(),
            "benchmark": {
                "dijkstra_cost": _fin(bench["cost"]),
                "dijkstra_expansions": bench["expansions"],
                "cost_match": optimal_match,
            },
        }

    def plan(self, planner_id: str, expected_snapshot: str | None) -> dict:
        rec = self._get_planner(planner_id)
        if expected_snapshot and expected_snapshot != rec.snap.snapshot_id:
            raise ApiError(
                409,
                "snapshot_conflict",
                "规划器绑定的快照与 expected_snapshot 不一致",
            )
        diag = rec.planner.compute_shortest_path()
        return self._plan_result(rec, diag, map_changed=False)

    def move_start(
        self, planner_id: str, new_start: tuple[int, int], expected_snapshot: str | None
    ) -> dict:
        rec = self._get_planner(planner_id)
        if expected_snapshot and expected_snapshot != rec.snap.snapshot_id:
            raise ApiError(409, "snapshot_conflict", "规划器绑定的快照不匹配")
        view = rec.view
        self._check_endpoint(view, new_start, "start")
        rec.start = new_start
        try:
            diag = rec.planner.move_start(new_start)
        except PlannerError as e:
            raise ApiError(422, "planner_error", str(e))
        return self._plan_result(rec, diag, map_changed=False)

    def update_planner_map(
        self,
        planner_id: str,
        changes: list[dict],
        expected_snapshot: str | None,
    ) -> dict:
        """在规划器绑定的地图链上提交新版本, 并做增量修复。"""
        rec = self._get_planner(planner_id)
        if expected_snapshot and expected_snapshot != rec.snap.snapshot_id:
            raise ApiError(
                409,
                "snapshot_conflict",
                "变更必须基于规划器当前绑定的快照",
            )
        snap = self.update_map(
            rec.map_id,
            changes,
            rec.snap.snapshot_id,
            freeze_cells=[rec.start, rec.goal],
        )
        view = self._make_view(snap, rec.store)
        changed_cells = [(int(c["row"]), int(c["col"])) for c in changes]
        try:
            diag = rec.planner.update_grid(view, changed_cells)
        except PlannerError as e:
            raise ApiError(422, "planner_error", str(e))
        rec.snap = snap
        return self._plan_result(rec, diag, map_changed=True)

    def reset_planner(
        self,
        planner_id: str,
        snapshot_id: str | None,
        new_start: tuple[int, int] | None,
    ) -> dict:
        """硬重置: 放弃全部增量状态。默认重置到当前绑定快照(与初始等价),
        可指定目标快照(必须是该地图链上的版本, 用于 rebase 到新版本)。"""
        rec = self._get_planner(planner_id)
        old_snap = rec.snap
        try:
            snap = (
                old_snap
                if snapshot_id is None
                else rec.store.require(snapshot_id)
            )
        except SnapshotNotFound:
            raise ApiError(404, "snapshot_not_found", f"快照不存在: {snapshot_id}")
        start = new_start or rec.start
        view = self._make_view(snap, rec.store)
        self._check_endpoint(view, start, "start")
        self._check_endpoint(view, rec.goal, "goal")
        map_changed = snap.snapshot_id != old_snap.snapshot_id
        rec.start = start
        rec.snap = snap
        try:
            rec.planner = DStarLite(view, start, rec.goal)
        except PlannerError as e:
            raise ApiError(422, "planner_error", str(e))
        diag = rec.planner.last_diag
        return self._plan_result(rec, diag, map_changed=map_changed)


def _fin(x):
    return None if (x is None or np.isinf(x) or np.isnan(x)) else float(x)
