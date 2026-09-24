"""服务编排层：版本快照 + 锁外搜索 + 提交前重新校验（乐观并发）。

一次 /reservations 规划的生命周期：

1. 读快照（短临界区）：地图、地图版本、预订版本、全部激活预订。
2. 校验 expected_map_version / expected_reservation_version（陈旧立即 409，
   把当前版本作为冲突证据返回）。
3. 在锁外执行时空 A*（耗时计算不阻塞其他地图的请求）。
4. 进入写事务重新取版本并对*最新*预订集做整条路径 revalidate：
   - 地图版本在规划期间变化 -> MAP_VERSION_MISMATCH（用新地图重新规划是调用方的
     责任，服务不悄悄替调用方在新地图上落预订）；
   - 预订冲突（并发请求抢先）-> 回滚，最多重规划 MAX_ATTEMPTS 次，仍失败则
     返回真实冲突证据；
   - 通过 -> 原子替换旧预订、res_version+1、写审计。
"""

from __future__ import annotations

import json
import sqlite3
import threading
from dataclasses import dataclass
from typing import Any, Callable

from . import crypto
from .database import Database
from .errors import BadRequest, Conflict, Forbidden, NotFound
from .planner import (
    Constraints,
    Endpoint,
    StaticMap,
    manhattan,
    plan_path,
    revalidate_path,
)

Cell = tuple[int, int]
MAX_ROBOTS = 8
MAX_HORIZON = 64
MAX_ATTEMPTS = 3  # 总尝试次数（含首次搜索）

# 测试钩子：A* 搜索开始时调用（参数为 map_id），用于制造"规划期间地图变化"。
pre_search_hook: Callable[[str], None] | None = None


@dataclass
class Snapshot:
    map_id: str
    width: int
    height: int
    obstacles: frozenset[Cell]
    map_version: int
    content_hash: str
    res_version: int
    rows: list[sqlite3.Row]


class PlannerService:
    def __init__(self, db: Database):
        self.db = db

    # ------------------------------------------------------------- helpers
    def _require_map(self, map_id: str) -> sqlite3.Row:
        row = self.db.get_map(map_id)
        if row is None:
            raise NotFound(f"map '{map_id}' does not exist",
                           {"map_id": map_id})
        return row

    @staticmethod
    def _validate_robot_id(robot_id: int) -> None:
        if not isinstance(robot_id, int) or not (1 <= robot_id <= MAX_ROBOTS):
            raise BadRequest(
                f"robot_id must be an integer in [1,{MAX_ROBOTS}]",
                {"robot_id": robot_id, "max_robots": MAX_ROBOTS},
            )

    @staticmethod
    def _validate_cell(cell: list[int], width: int, height: int,
                       name: str) -> Cell:
        if (not isinstance(cell, (list, tuple)) or len(cell) != 2
                or not all(isinstance(v, int) and not isinstance(v, bool)
                           for v in cell)):
            raise BadRequest(f"{name} must be [x, y] integers", {name: cell})
        c = (int(cell[0]), int(cell[1]))
        if not (0 <= c[0] < width and 0 <= c[1] < height):
            raise BadRequest(
                f"{name} {c} out of bounds for {width}x{height} map",
                {name: list(c), "width": width, "height": height},
            )
        return c

    def _snapshot(self, map_id: str) -> Snapshot:
        """短临界区读取一致性快照（map 元数据 + 全部激活预订）。"""
        with self.db.write_lock:
            mrow = self._require_map(map_id)
            rows = self.db.active_reservations(map_id)
            return Snapshot(
                map_id=map_id,
                width=mrow["width"],
                height=mrow["height"],
                obstacles=frozenset(tuple(c) for c in json.loads(mrow["obstacles"])),
                map_version=mrow["map_version"],
                content_hash=mrow["content_hash"],
                res_version=mrow["res_version"],
                rows=list(rows),
            )

    @staticmethod
    def _build_constraints(
        rows: Iterable[sqlite3.Row],
        *,
        exclude_robots: set[int] | None = None,
    ) -> tuple[Constraints, int]:
        """把激活预订行展开为规划约束。exclude_robots 的旧预订将被替换，排除。"""
        exclude_robots = exclude_robots or set()
        vertices: dict[tuple[Cell, int], int] = {}
        edges: dict[tuple[int, Cell, Cell], tuple[int, Cell, Cell]] = {}
        endpoints: dict[int, Endpoint] = {}
        max_time = 0
        for row in rows:
            rid = row["robot_id"]
            if rid in exclude_robots:
                continue
            path: list[list[int]] = json.loads(row["path"])
            cells = [(c[0], c[1]) for c in path]
            for t, cell in enumerate(cells):
                vertices.setdefault((cell, t), rid)
            for t, (u, v) in enumerate(zip(cells, cells[1:])):
                if u == v:
                    continue
                a, b = (u, v) if u <= v else (v, u)
                edges.setdefault((t, a, b), (rid, u, v))
            arrival = len(cells) - 1
            endpoints[rid] = Endpoint(rid, cells[-1], arrival)
            max_time = max(max_time, row["horizon"], arrival)
        return Constraints(vertices=vertices, edges=edges,
                           endpoints=endpoints, max_time=max_time), max_time

    @staticmethod
    def _default_horizon(start: Cell, goal: Cell, existing_max: int) -> int:
        need = manhattan(start, goal) + 2
        return min(MAX_HORIZON, max(existing_max, need))

    def _reservation_payload(
        self, map_id: str, row: sqlite3.Row
    ) -> dict[str, Any]:
        return {
            "reservation_id": row["reservation_id"],
            "map_id": map_id,
            "robot_id": row["robot_id"],
            "horizon": row["horizon"],
            "path": json.loads(row["path"]),
            "actions": json.loads(row["actions"]),
            "status": row["status"],
            "created_at": row["created_at"],
        }

    def _map_payload(self, row: sqlite3.Row) -> dict[str, Any]:
        return {
            "map_id": row["map_id"],
            "width": row["width"],
            "height": row["height"],
            "obstacles": json.loads(row["obstacles"]),
            "map_version": row["map_version"],
            "content_hash": row["content_hash"],
            "reservation_version": row["res_version"],
            "created_at": row["created_at"],
        }

    # ---------------------------------------------------------------- maps
    def create_map(
        self,
        map_id: str,
        width: int,
        height: int,
        obstacles: list[list[int]],
    ) -> dict[str, Any]:
        if not (1 <= width <= 1000 and 1 <= height <= 1000):
            raise BadRequest("width/height must be in [1,1000]",
                             {"width": width, "height": height})
        obs = set()
        for c in obstacles:
            cell = self._validate_cell(c, width, height, "obstacle")
            if cell in obs:
                raise BadRequest(f"duplicate obstacle {cell}", {"cell": list(cell)})
            obs.add(cell)
        chash = crypto.content_hash(width, height, list(obs))
        with self.db.write_lock:
            if self.db.get_map(map_id) is not None:
                raise Conflict(f"map '{map_id}' already exists",
                               {"map_id": map_id})
            self.db.create_map(map_id, width, height, list(obs), chash)
            self.db.audit("map_created", {"width": width, "height": height,
                                          "obstacles": len(obs)}, map_id=map_id)
            row = self.db.get_map(map_id)
        return self._map_payload(row)

    def get_map(self, map_id: str) -> dict[str, Any]:
        return self._map_payload(self._require_map(map_id))

    def update_obstacles(
        self, map_id: str, obstacles: list[list[int]]
    ) -> dict[str, Any]:
        with self.db.write_lock:
            mrow = self._require_map(map_id)
            width, height = mrow["width"], mrow["height"]
            obs = set()
            for c in obstacles:
                cell = self._validate_cell(c, width, height, "obstacle")
                obs.add(cell)
            chash = crypto.content_hash(width, height, list(obs))
            self.db.update_map_obstacles(map_id, list(obs), chash)
            self.db.audit("map_obstacles_updated",
                          {"obstacles": sorted(list(obs)),
                           "new_map_version": mrow["map_version"] + 1},
                          map_id=map_id)
            return self._map_payload(self.db.get_map(map_id))

    # ------------------------------------------------------------- planning
    def _check_expected_versions(
        self,
        snap: Snapshot,
        expected_map_version: int | None,
        expected_res_version: int | None,
    ) -> None:
        if (expected_map_version is not None
                and expected_map_version != snap.map_version):
            raise Conflict(
                "expected_map_version does not match current map; map changed, "
                "re-fetch and replan",
                {
                    "error_subtype": "MAP_VERSION_MISMATCH",
                    "expected_map_version": expected_map_version,
                    "current_map_version": snap.map_version,
                    "content_hash": snap.content_hash,
                    "current_reservation_version": snap.res_version,
                },
            )
        if (expected_res_version is not None
                and expected_res_version != snap.res_version):
            raise Conflict(
                "expected_reservation_version is stale; re-fetch reservations "
                "and replan",
                {
                    "error_subtype": "RESERVATION_VERSION_STALE",
                    "expected_reservation_version": expected_res_version,
                    "current_reservation_version": snap.res_version,
                    "current_map_version": snap.map_version,
                },
            )

    def plan_one(
        self,
        map_id: str,
        robot_id: int,
        start: list[int] | None,
        goal: list[int],
        horizon: int | None,
        expected_map_version: int | None,
        expected_res_version: int | None,
    ) -> dict[str, Any]:
        """单个机器人的规划+预订（含乐观重试循环）。"""
        self._validate_robot_id(robot_id)
        mrow = self._require_map(map_id)
        width, height = mrow["width"], mrow["height"]
        goal_cell = self._validate_cell(goal, width, height, "goal")

        # start 缺省：该机器人已有预订时沿用其当前位置，否则报错
        with self.db.write_lock:
            existing = [
                r for r in self.db.active_reservations(map_id)
                if r["robot_id"] == robot_id
            ]
        if start is None:
            if not existing:
                raise BadRequest(
                    "start is required when the robot has no active reservation",
                    {"robot_id": robot_id},
                )
            start_cell: Cell = tuple(json.loads(existing[0]["path"])[0])  # type: ignore
        else:
            start_cell = self._validate_cell(start, width, height, "start")

        last_evidence: dict[str, Any] | None = None
        for attempt in range(1, MAX_ATTEMPTS + 1):
            snap = self._snapshot(map_id)
            self._check_expected_versions(
                snap, expected_map_version, expected_res_version)
            grid = StaticMap(snap.width, snap.height, snap.obstacles)
            constraints, existing_max = self._build_constraints(
                snap.rows, exclude_robots={robot_id})
            h = (min(MAX_HORIZON, max(1, int(horizon))) if horizon is not None
                 else self._default_horizon(start_cell, goal_cell, existing_max))
            if horizon is not None and not (1 <= int(horizon) <= MAX_HORIZON):
                raise BadRequest(
                    f"horizon must be in [1,{MAX_HORIZON}]", {"horizon": horizon})

            # 锁外执行计算（测试钩子在此刻可以改动地图，模拟规划期间地图变化）
            if pre_search_hook is not None and attempt == 1:
                pre_search_hook(map_id)

            result = plan_path(grid, start_cell, goal_cell, h,
                               constraints, robot_id)
            if not result.ok:
                # 规划空间本身无解（基于本次快照即确定的结构性冲突）：
                # 不重试，直接以 409 返回证据。
                raise Conflict(
                    result.evidence["message"],
                    {
                        "error_subtype": result.evidence["type"],
                        "evidence": result.evidence,
                        "robot_id": robot_id,
                        "map_version": snap.map_version,
                        "reservation_version": snap.res_version,
                        "attempts": attempt,
                        "policy": {
                            "priority": "fixed by robot_id ascending "
                                          "(1 highest)",
                            "globally_complete": False,
                            "reason": "fixed-priority sequential planning can "
                                        "block lower-priority robots even when "
                                        "a joint solution exists; cancel or "
                                        "adjust a higher-priority reservation "
                                        "and retry",
                        },
                    },
                )

            # ---- 锁内提交：重新取版本 + 用最新预订整条重校验 ----
            outcome = self._try_commit(
                snap, robot_id, start_cell, goal_cell, h, result,
                expected_map_version=expected_map_version)
            if outcome["committed"]:
                outcome["attempts"] = attempt
                return outcome
            last_evidence = outcome["evidence"]
            # 地图版本在规划期间变化 -> 明确要求重新校验/重规划，不静默重试
            if outcome["evidence"].get("error_subtype") == "MAP_VERSION_MISMATCH":
                raise Conflict(
                    "map changed during planning; re-fetch and replan",
                    outcome["evidence"],
                )
            # 预订版本竞争：换最新快照重规划（仍遵守调用方给的地图版本前提）
            continue

        raise Conflict(
            "planning conflict persisted after retries",
            {"error_subtype": "RETRY_LIMIT",
             "attempts": MAX_ATTEMPTS, "evidence": last_evidence},
        )

    def _try_commit(
        self,
        snap: Snapshot,
        robot_id: int,
        start: Cell,
        goal: Cell,
        horizon: int,
        result,
        *,
        expected_map_version: int | None,
    ) -> dict[str, Any]:
        """尝试原子提交。返回 committed=True 的结果，或 conflict 证据。"""
        with self.db.write_lock:
            mrow = self.db.get_map(snap.map_id)
            current_map_version = mrow["map_version"]
            if current_map_version != snap.map_version:
                # 规划期间地图发生变化：路径未在新地图上校验，必须重新规划
                return {"committed": False, "evidence": {
                    "error_subtype": "MAP_VERSION_MISMATCH",
                    "type": "MAP_VERSION_MISMATCH",
                    "message": "map changed during planning",
                    "expected_map_version": (expected_map_version
                                             or snap.map_version),
                    "current_map_version": current_map_version,
                    "content_hash": mrow["content_hash"],
                }}
            latest_rows = self.db.active_reservations(snap.map_id)
            constraints, latest_max = self._build_constraints(
                latest_rows, exclude_robots={robot_id})
            grid = StaticMap(snap.width, snap.height, snap.obstacles)
            ev = revalidate_path(
                grid, result.path, constraints, robot_id,
                window_end=max(horizon, latest_max))
            if ev is not None:
                ev["current_reservation_version"] = mrow["res_version"]
                return {"committed": False, "evidence": ev}

            reservation_id = f"rsv_{crypto.random_token(8)}"
            token = crypto.random_token(32)
            token_hash = crypto.hash_token(token)
            try:
                self.db.conn.execute("BEGIN IMMEDIATE")
                self.db.delete_active_reservation_rows(snap.map_id, robot_id)
                self.db.insert_reservation(
                    reservation_id=reservation_id,
                    map_id=snap.map_id,
                    robot_id=robot_id,
                    horizon=horizon,
                    path=result.path,
                    actions=result.actions,
                    token_hash=token_hash,
                )
                new_res_version = self.db.bump_res_version(snap.map_id)
                self.db.conn.execute("COMMIT")
            except sqlite3.IntegrityError as exc:
                self.db.conn.execute("ROLLBACK")
                return {"committed": False, "evidence": {
                    "error_subtype": "COMMIT_RACE",
                    "type": "EDGE_OR_VERTEX_CONFLICT",
                    "message": f"conforming constraint rejected at commit: {exc}",
                }}
            except Exception:
                self.db.conn.execute("ROLLBACK")
                raise

            self.db.audit(
                "reservation_created",
                {"robot_id": robot_id, "start": list(start), "goal": list(goal),
                 "horizon": horizon, "path_len": len(result.path),
                 "nodes_explored": result.nodes_explored,
                 "prune_counts": result.prune_counts},
                map_id=snap.map_id, reservation_id=reservation_id)
            row = self.db.get_reservation(reservation_id)
            payload = self._reservation_payload(snap.map_id, row)
            payload.update({
                "planned": True,
                "map_version": current_map_version,
                "reservation_version": new_res_version,
                "cancel_token": token,  # 仅本次创建响应返回明文凭证
                "search": {
                    "nodes_explored": result.nodes_explored,
                    "prune_counts": result.prune_counts,
                },
                "policy": {
                    "priority": "fixed by robot_id ascending (1 highest)",
                    "globally_complete": False,
                    "reason": "fixed-priority sequential planning can block "
                              "lower-priority robots even when a joint "
                              "solution exists",
                },
            })
            return {"committed": True, **payload}

    def batch_plan(
        self,
        map_id: str,
        requests: list[dict[str, Any]],
        expected_map_version: int | None,
        expected_res_version: int | None,
    ) -> dict[str, Any]:
        """联合规划一批机器人（至多 8 个），固定优先级 = robot_id 升序。

        批内按优先级顺序搜索；每个机器人搜索时把批内更高优先级的*候选路径*
        一并作为约束。整批原子提交：任一机器人失败则全部不生效，返回其
        冲突证据（体现固定优先级不保证全局完备）。
        """
        if not requests:
            raise BadRequest("requests must be non-empty")
        if len(requests) > MAX_ROBOTS:
            raise BadRequest(f"batch size exceeds {MAX_ROBOTS}",
                             {"size": len(requests)})
        for req in requests:
            self._validate_robot_id(req["robot_id"])
        ids = [r["robot_id"] for r in requests]
        if len(set(ids)) != len(ids):
            raise BadRequest("duplicate robot_id in batch", {"robot_ids": ids})

        mrow = self._require_map(map_id)
        width, height = mrow["width"], mrow["height"]
        ordered = sorted(requests, key=lambda r: r["robot_id"])
        parsed = []
        for req in ordered:
            goal_cell = self._validate_cell(req["goal"], width, height, "goal")
            if req.get("start") is None:
                raise BadRequest("start is required for every batch request",
                                 {"robot_id": req["robot_id"]})
            start_cell = self._validate_cell(req["start"], width, height, "start")
            parsed.append((req["robot_id"], start_cell, goal_cell,
                           req.get("horizon")))

        snap = self._snapshot(map_id)
        self._check_expected_versions(
            snap, expected_map_version, expected_res_version)
        grid = StaticMap(snap.width, snap.height, snap.obstacles)
        base_constraints, existing_max = self._build_constraints(
            snap.rows, exclude_robots=set(ids))

        # 用批内候选路径增量扩展约束
        live_vertices = dict(base_constraints.vertices)
        live_edges = dict(base_constraints.edges)
        live_endpoints = dict(base_constraints.endpoints)
        results: list[dict[str, Any]] = []
        window_end = existing_max
        candidate_paths: dict[int, list[Cell]] = {}

        for robot_id, start_cell, goal_cell, horizon_req in parsed:
            h = (min(MAX_HORIZON, max(1, int(horizon_req)))
                 if horizon_req is not None
                 else self._default_horizon(start_cell, goal_cell, window_end))
            cons = Constraints(
                vertices=live_vertices, edges=live_edges,
                endpoints=live_endpoints, max_time=window_end)
            res = plan_path(grid, start_cell, goal_cell, h, cons, robot_id)
            if not res.ok:
                raise Conflict(
                    res.evidence["message"],
                    {
                        "error_subtype": res.evidence["type"],
                        "evidence": res.evidence,
                        "failed_robot": robot_id,
                        "map_version": snap.map_version,
                        "reservation_version": snap.res_version,
                        "results_so_far": results,
                        "policy": {
                            "priority": "fixed by robot_id ascending "
                                          "(1 highest)",
                            "globally_complete": False,
                            "reason": "lower-priority robot failed under "
                                        "fixed priority; a different joint "
                                        "plan might exist",
                        },
                    },
                )
            path = res.path
            candidate_paths[robot_id] = path
            for t, cell in enumerate(path):
                live_vertices.setdefault((cell, t), robot_id)
            for t, (u, v) in enumerate(zip(path, path[1:])):
                if u != v:
                    a, b = (u, v) if u <= v else (v, u)
                    live_edges.setdefault((t, a, b), (robot_id, u, v))
            live_endpoints[robot_id] = Endpoint(
                robot_id, path[-1], len(path) - 1)
            window_end = max(window_end, h)
            results.append({
                "robot_id": robot_id,
                "planned": True,
                "path": [list(c) for c in path],
                "actions": res.actions,
                "horizon": h,
            })

        # ---- 整批原子提交：提交点再次核对地图版本（规划期间地图变化检测）----
        with self.db.write_lock:
            mrow2 = self.db.get_map(map_id)
            if mrow2["map_version"] != snap.map_version:
                raise Conflict(
                    "map changed during batch planning; re-fetch and replan",
                    {"error_subtype": "MAP_VERSION_MISMATCH",
                     "current_map_version": mrow2["map_version"],
                     "expected_map_version": snap.map_version},
                )
            # 用最新库内预订校验全部候选路径（应对并发）
            latest_rows = self.db.active_reservations(map_id)
            other_constraints, latest_max = self._build_constraints(
                latest_rows, exclude_robots=set(ids))
            for robot_id, path in candidate_paths.items():
                # 叠加批内其他候选
                cons = Constraints(
                    vertices=dict(other_constraints.vertices),
                    edges=dict(other_constraints.edges),
                    endpoints=dict(other_constraints.endpoints),
                    max_time=latest_max)
                for rid2, p2 in candidate_paths.items():
                    if rid2 == robot_id:
                        continue
                    for t, cell in enumerate(p2):
                        cons.vertices.setdefault((cell, t), rid2)
                    for t, (u, v) in enumerate(zip(p2, p2[1:])):
                        if u != v:
                            a, b = (u, v) if u <= v else (v, u)
                            cons.edges.setdefault((t, a, b), (rid2, u, v))
                    cons.endpoints[rid2] = Endpoint(
                        rid2, p2[-1], len(p2) - 1)
                ev = revalidate_path(
                    StaticMap(snap.width, snap.height, snap.obstacles),
                    path, cons, robot_id, window_end=max(window_end, latest_max))
                if ev is not None:
                    raise Conflict(
                        "batch commit conflict with a concurrent reservation",
                        {"error_subtype": "COMMIT_RACE",
                         "robot_id": robot_id, "evidence": ev})

            tokens: dict[int, str] = {}
            try:
                self.db.conn.execute("BEGIN IMMEDIATE")
                for robot_id, path in candidate_paths.items():
                    self.db.delete_active_reservation_rows(map_id, robot_id)
                    token = crypto.random_token(32)
                    tokens[robot_id] = token
                    h_used = next(r["horizon"] for r in results
                                  if r["robot_id"] == robot_id)
                    actions = [
                        {"tick": t,
                         "type": "wait" if u == v else "move",
                         "from": list(u), "to": list(v)}
                        for t, (u, v) in enumerate(zip(path, path[1:]))]
                    self.db.insert_reservation(
                        reservation_id=f"rsv_{crypto.random_token(8)}",
                        map_id=map_id, robot_id=robot_id, horizon=h_used,
                        path=path, actions=actions,
                        token_hash=crypto.hash_token(token))
                new_res_version = self.db.bump_res_version(map_id)
                self.db.conn.execute("COMMIT")
            except sqlite3.IntegrityError as exc:
                self.db.conn.execute("ROLLBACK")
                raise Conflict(
                    "conforming constraint rejected at commit",
                    {"error_subtype": "COMMIT_RACE", "message": str(exc)})
            except Exception:
                self.db.conn.execute("ROLLBACK")
                raise

            for rid, tok in tokens.items():
                for r in results:
                    if r["robot_id"] == rid:
                        r["cancel_token"] = tok
            self.db.audit(
                "batch_reservations_created",
                {"robots": ids, "count": len(ids)}, map_id=map_id)
            return {
                "planned": True,
                "map_version": snap.map_version,
                "reservation_version": new_res_version,
                "results": results,
                "policy": {
                    "priority": "fixed by robot_id ascending (1 highest)",
                    "globally_complete": False,
                },
            }

    # ------------------------------------------------------------ cancel etc
    def cancel(self, map_id: str, robot_id: int, token: str) -> dict[str, Any]:
        self._validate_robot_id(robot_id)
        self._require_map(map_id)
        with self.db.write_lock:
            rows = self.db.active_reservations(map_id)
            match = [r for r in rows if r["robot_id"] == robot_id]
            if not match:
                raise NotFound(
                    f"no active reservation for robot {robot_id}",
                    {"map_id": map_id, "robot_id": robot_id})
            row = match[0]
            if not crypto.verify_token(token, row["token_hash"]):
                self.db.audit("cancel_denied_bad_token",
                              {"robot_id": robot_id}, map_id=map_id,
                              reservation_id=row["reservation_id"])
                raise Forbidden("invalid cancel token", {"robot_id": robot_id})
            reservation_id = row["reservation_id"]
            self.db.cancel_reservation(reservation_id)
            new_res_version = self.db.bump_res_version(map_id)
            self.db.audit("reservation_cancelled", {"robot_id": robot_id},
                          map_id=map_id, reservation_id=reservation_id)
        return {"cancelled": True, "reservation_id": reservation_id,
                "robot_id": robot_id,
                "reservation_version": new_res_version}

    def list_reservations(self, map_id: str) -> dict[str, Any]:
        self._require_map(map_id)
        with self.db.write_lock:
            rows = self.db.active_reservations(map_id)
            mrow = self.db.get_map(map_id)
            return {
                "map_id": map_id,
                "map_version": mrow["map_version"],
                "reservation_version": mrow["res_version"],
                "reservations": [self._reservation_payload(map_id, r)
                                 for r in rows],
            }

    def debug_state(self, map_id: str) -> dict[str, Any]:
        self._require_map(map_id)
        data = self.list_reservations(map_id)
        mrow = self._require_map(map_id)
        obstacles = {tuple(c) for c in json.loads(mrow["obstacles"])}
        grid = [["."] * mrow["width"] for _ in range(mrow["height"])]
        for (x, y) in obstacles:
            grid[y][x] = "#"
        for rsv in data["reservations"]:
            x, y = rsv["path"][-1]
            grid[y][x] = str(rsv["robot_id"])
        ascii_lines = ["+" + "-" * mrow["width"] + "+"]
        for row in reversed(grid):
            ascii_lines.append("|" + "".join(row) + "|")
        ascii_lines.append("+" + "-" * mrow["width"] + "+")
        data["ascii_endpoints"] = ascii_lines
        return data
