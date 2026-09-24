"""业务服务层：快照读取 -> 时空 A* 规划 -> 版本复核后原子提交。

并发协议（乐观并发控制）
========================
每个规划请求携带两个"预期版本"：

* ``expected_map_version``：请求方认为的地图版本；
* ``expected_reservation_version``：请求方认为的预约版本（全局单调递增整数，
  每次成功提交/撤销预约都 +1）。

处理流程：

1. 读一致性快照（地图、机器人、所有活跃预约及其占用、两个版本号）；
2. 若客户端给了预期版本，与快照不符立即 412，要求重读后重试；
3. 在快照上跑 CPU 密集的时空 A*（事务外，不持锁）；
4. 提交时开 ``BEGIN IMMEDIATE`` 写事务，**重新**比对地图与预约版本，并在最新地图、
   最新预约上逐条复核路径（顶点 + 边 + 永久终点）。若这期间地图被改过（地图版本变
   了）或预约表被别的请求改过（预约版本变了），返回 412 冲突证据，请客户端用新地
   图/新预约重新规划；
5. 复核通过才写入占用行；``vertex_res`` 的主键在数据库层兜底防止并发写入同刻同格。
"""

from __future__ import annotations

import sqlite3
import uuid
from typing import Callable, Dict, List, Optional, Tuple

from . import db as dbmod
from .crypto import TokenError, issue_token, verify_token
from .planner import (
    Cell,
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

DEFAULT_HORIZON = 200
DEFAULT_MAX_NODES = 200_000


class ServiceError(Exception):
    def __init__(self, status: int, code: str, message: str, details: Optional[dict] = None):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.details = details or {}


# ---------------------------------------------------------------------- 约束构建


def build_constraints(
    active: List[dict], ignore_robot_ids: Optional[set] = None
) -> Constraints:
    """把数据库中的活跃预约转换为规划约束。

    每个预约的逐刻路径产生顶点占用与移动边；其终点在 arrival_time 之后永久占用。
    """
    ignore = ignore_robot_ids or set()
    cons = Constraints()
    for r in active:
        if r["robot_id"] in ignore:
            continue
        for k, cell in enumerate(r["path"]):
            t = r["start_time"] + k
            cons.vertex.add(VertexBlock(cell, t, f"reservation:{r['resv_id']}/{r['robot_id']}"))
        for t, src, dst in r["edges"]:
            cons.edges.add(
                EdgeBlock(src, dst, t, f"reservation:{r['resv_id']}/{r['robot_id']}")
            )
        cons.permanent.add(
            PermanentBlock(
                r["goal"],
                r["arrival_time"],
                f"reservation:{r['resv_id']}/{r['robot_id']}",
            )
        )
    return cons


# ---------------------------------------------------------------------- 快照


class Snapshot:
    def __init__(self, conn: sqlite3.Connection):
        self.grid, self.map_version = dbmod.load_map(conn)
        self.robots = dbmod.load_robots(conn)
        self.active = dbmod.load_active_reservations(conn)
        self.reservation_version = dbmod.get_reservation_version(conn)
        self.secret = dbmod.get_server_secret(conn)
        self.active_by_robot = {r["robot_id"]: r for r in self.active}


def _check_expected(
    snap: Snapshot,
    expected_map_version: Optional[int],
    expected_resv_version: Optional[int],
) -> None:
    if expected_map_version is not None and expected_map_version != snap.map_version:
        raise ServiceError(
            412,
            "MAP_STALE",
            f"预期地图版本 {expected_map_version} 与当前版本 {snap.map_version} 不一致",
            {
                "expected_map_version": expected_map_version,
                "current_map_version": snap.map_version,
            },
        )
    if (
        expected_resv_version is not None
        and expected_resv_version != snap.reservation_version
    ):
        raise ServiceError(
            412,
            "RESV_STALE",
            f"预期预约版本 {expected_resv_version} 与当前版本 {snap.reservation_version} 不一致",
            {
                "expected_reservation_version": expected_resv_version,
                "current_reservation_version": snap.reservation_version,
            },
        )


def _actions_from_path(path: List[Cell], start_time: int) -> List[dict]:
    actions = []
    for k in range(1, len(path)):
        src, dst = path[k - 1], path[k]
        actions.append(
            {
                "time": start_time + k - 1,
                "type": "wait" if src == dst else "move",
                "from": [src[0], src[1]],
                "to": [dst[0], dst[1]],
            }
        )
    return actions


def _plan_payload(
    resv_id: str,
    robot_id: str,
    path: List[Cell],
    start_time: int,
    map_version: int,
    token: str,
) -> dict:
    return {
        "resv_id": resv_id,
        "robot_id": robot_id,
        "path": [[x, y] for x, y in path],
        "actions": _actions_from_path(path, start_time),
        "start_time": start_time,
        "arrival_time": start_time + len(path) - 1,
        "goal": [path[-1][0], path[-1][1]],
        "map_version": map_version,
        "cancel_token": token,
    }


# ---------------------------------------------------------------------- 单个规划


def plan_single(
    conn_factory: Callable[[], sqlite3.Connection],
    *,
    robot_id: str,
    goal: Cell,
    expected_map_version: Optional[int] = None,
    expected_resv_version: Optional[int] = None,
    horizon: int = DEFAULT_HORIZON,
    max_nodes: int = DEFAULT_MAX_NODES,
    before_search_hook: Optional[Callable[[Snapshot], None]] = None,
) -> dict:
    """为单个机器人规划并提交预约。"""
    conn = conn_factory()
    try:
        snap = Snapshot(conn)
        _check_expected(snap, expected_map_version, expected_resv_version)

        if robot_id not in snap.robots:
            raise ServiceError(404, "ROBOT_NOT_FOUND", f"机器人 {robot_id} 不存在")
        start = snap.robots[robot_id]
        if robot_id in snap.active_by_robot:
            r = snap.active_by_robot[robot_id]
            raise ServiceError(
                409,
                "ROBOT_BUSY",
                f"机器人 {robot_id} 已有活跃预约 {r['resv_id']}，请先撤销再规划",
                {"resv_id": r["resv_id"], "arrival_time": r["arrival_time"]},
            )
        if not snap.grid.passable(start):
            raise ServiceError(
                409,
                "START_OBSTRUCTED",
                f"机器人 {robot_id} 的起点 {start} 在当前地图上不可通行",
                {"cell": list(start), "map_version": snap.map_version},
            )
        goal = (int(goal[0]), int(goal[1]))
        if not snap.grid.passable(goal):
            raise ServiceError(
                400,
                "GOAL_OBSTRUCTED",
                f"目标格 {goal} 是障碍或越界",
                {"cell": list(goal)},
            )

        cons = build_constraints(snap.active, ignore_robot_ids={robot_id})

        # 测试/调试缝隙：快照读取之后、搜索之前执行；可借此在另一个连接上提交地图
        # 变更，以确定性地验收"规划期间地图变化须重新校验"。
        if before_search_hook is not None:
            before_search_hook(snap)

        result = plan_one(
            snap.grid,
            start,
            goal,
            cons,
            start_time=0,
            horizon=horizon,
            max_nodes=max_nodes,
            actor_ref=robot_id,
        )
        if not result.found:
            raise ServiceError(
                409,
                "NO_FEASIBLE_PATH",
                f"在固定优先级与当前预约下找不到 {robot_id} 到 {goal} 的无冲突路径；"
                "注意这是贪婪、非完备规划器，失败不等于全局无解",
                {
                    "robot_id": robot_id,
                    "start": list(start),
                    "goal": list(goal),
                    "evidence": result.evidence.to_json(),  # type: ignore[union-attr]
                    "expanded_nodes": result.expanded_nodes,
                    "horizon": result.horizon,
                    "snapshot_map_version": snap.map_version,
                    "snapshot_reservation_version": snap.reservation_version,
                },
            )
        path = result.path

        return _commit_single(
            conn_factory,
            robot_id=robot_id,
            start=start,
            path=path,
            snap_map_version=snap.map_version,
            snap_resv_version=snap.reservation_version,
            secret=snap.secret,
        )
    finally:
        conn.close()


def _commit_single(
    conn_factory: Callable[[], sqlite3.Connection],
    *,
    robot_id: str,
    start: Cell,
    path: List[Cell],
    snap_map_version: int,
    snap_resv_version: int,
    secret: str,
) -> dict:
    conn = conn_factory()
    try:
        with dbmod.immediate_tx(conn):
            cur_grid, cur_map_version = dbmod.load_map(conn)
            cur_resv_version = dbmod.get_reservation_version(conn)
            if cur_map_version != snap_map_version:
                raise ServiceError(
                    412,
                    "MAP_CHANGED_DURING_PLANNING",
                    f"规划期间地图已从版本 {snap_map_version} 变为 {cur_map_version}，"
                    "规划作废，请基于新地图重新规划",
                    {
                        "snapshot_map_version": snap_map_version,
                        "current_map_version": cur_map_version,
                    },
                )
            if cur_resv_version != snap_resv_version:
                raise ServiceError(
                    412,
                    "RESV_CHANGED_DURING_PLANNING",
                    f"规划期间预约表已从版本 {snap_resv_version} 变为 {cur_resv_version}，"
                    "规划作废，请重读预约后重新规划",
                    {
                        "snapshot_reservation_version": snap_resv_version,
                        "current_reservation_version": cur_resv_version,
                    },
                )

            active = dbmod.load_active_reservations(conn)
            cons = build_constraints(active, ignore_robot_ids={robot_id})
            conflict = validate_path(cur_grid, start, path, cons, start_time=0)
            if conflict is not None:
                raise ServiceError(
                    412,
                    "REVALIDATION_CONFLICT",
                    "提交前在最新状态上复核路径时发现冲突，规划作废",
                    {"conflict": conflict},
                )

            resv_id = uuid.uuid4().hex
            dbmod.insert_reservation(
                conn,
                resv_id=resv_id,
                robot_id=robot_id,
                path=path,
                start_time=0,
                arrival_time=len(path) - 1,
                map_version=cur_map_version,
            )
            new_version = dbmod.bump_reservation_version(conn)
            token = issue_token(secret, {"resv_id": resv_id, "robot_id": robot_id})
        payload = _plan_payload(resv_id, robot_id, path, 0, cur_map_version, token)
        payload["reservation_version"] = new_version
        return payload
    except sqlite3.IntegrityError as exc:
        raise ServiceError(
            409,
            "COMMIT_CONFLICT",
            f"提交时数据库层检测到时空占用冲突（并发预约竞争）：{exc}",
        )
    finally:
        conn.close()


# ---------------------------------------------------------------------- 批量规划


def plan_batch(
    conn_factory: Callable[[], sqlite3.Connection],
    *,
    goals: Dict[str, Cell],
    expected_map_version: Optional[int] = None,
    expected_resv_version: Optional[int] = None,
    horizon: int = DEFAULT_HORIZON,
    max_nodes: int = DEFAULT_MAX_NODES,
    before_search_hook: Optional[Callable[[Snapshot], None]] = None,
) -> dict:
    """固定优先级批量规划。优先级固定为机器人 id 升序，原子提交：全成功或全不写入。"""
    conn = conn_factory()
    try:
        snap = Snapshot(conn)
        _check_expected(snap, expected_map_version, expected_resv_version)

        for rid in goals:
            if rid not in snap.robots:
                raise ServiceError(404, "ROBOT_NOT_FOUND", f"机器人 {rid} 不存在")
        if len(goals) != len(set(goals)):
            raise ServiceError(400, "DUPLICATE_ROBOT", "批量请求中同一机器人出现多次")
        busy = sorted(set(goals) & set(snap.active_by_robot))
        if busy:
            raise ServiceError(
                409,
                "ROBOT_BUSY",
                f"机器人 {busy} 已有活跃预约，请先撤销",
                {"robots": busy},
            )

        ordered_ids = sorted(goals)
        requests: List[Tuple[str, Cell, Cell]] = []
        for rid in ordered_ids:
            start = snap.robots[rid]
            goal = (int(goals[rid][0]), int(goals[rid][1]))
            if not snap.grid.passable(start):
                raise ServiceError(
                    409,
                    "START_OBSTRUCTED",
                    f"机器人 {rid} 的起点 {start} 在当前地图上不可通行",
                    {"robot_id": rid, "cell": list(start)},
                )
            if not snap.grid.passable(goal):
                raise ServiceError(
                    400,
                    "GOAL_OBSTRUCTED",
                    f"机器人 {rid} 的目标格 {goal} 是障碍或越界",
                    {"robot_id": rid, "cell": list(goal)},
                )
            requests.append((rid, start, goal))

        base_cons = build_constraints(snap.active, ignore_robot_ids=set(goals))

        if before_search_hook is not None:
            before_search_hook(snap)

        planned, failure = prioritized_batch(
            snap.grid,
            requests,
            base_cons,
            start_time=0,
            horizon=horizon,
            max_nodes=max_nodes,
        )
        if failure is not None:
            raise ServiceError(
                409,
                "NO_FEASIBLE_PATH",
                f"批量规划在机器人 {failure.robot_id} 处失败（固定优先级、贪婪非完备）；"
                f"整个批次未提交。成功顺序上界为 {failure.robot_id} 之前的机器人",
                {
                    "failed_robot": failure.robot_id,
                    "priority_order": ordered_ids,
                    "failure": failure.to_json(),
                    "snapshot_map_version": snap.map_version,
                    "snapshot_reservation_version": snap.reservation_version,
                },
            )

        return _commit_batch(
            conn_factory,
            planned=planned,  # type: ignore[arg-type]
            ordered_ids=ordered_ids,
            starts={rid: snap.robots[rid] for rid in ordered_ids},
            snap_map_version=snap.map_version,
            snap_resv_version=snap.reservation_version,
            secret=snap.secret,
        )
    finally:
        conn.close()


def _commit_batch(
    conn_factory: Callable[[], sqlite3.Connection],
    *,
    planned: List,
    ordered_ids: List[str],
    starts: Dict[str, Cell],
    snap_map_version: int,
    snap_resv_version: int,
    secret: str,
) -> dict:
    conn = conn_factory()
    try:
        with dbmod.immediate_tx(conn):
            cur_grid, cur_map_version = dbmod.load_map(conn)
            cur_resv_version = dbmod.get_reservation_version(conn)
            if cur_map_version != snap_map_version:
                raise ServiceError(
                    412,
                    "MAP_CHANGED_DURING_PLANNING",
                    f"规划期间地图已从版本 {snap_map_version} 变为 {cur_map_version}，批次作废",
                    {
                        "snapshot_map_version": snap_map_version,
                        "current_map_version": cur_map_version,
                    },
                )
            if cur_resv_version != snap_resv_version:
                raise ServiceError(
                    412,
                    "RESV_CHANGED_DURING_PLANNING",
                    f"规划期间预约表已从版本 {snap_resv_version} 变为 {cur_resv_version}，批次作废",
                    {
                        "snapshot_reservation_version": snap_resv_version,
                        "current_reservation_version": cur_resv_version,
                    },
                )

            active = dbmod.load_active_reservations(conn)
            base = build_constraints(active, ignore_robot_ids=set(ordered_ids))

            # 逐条按优先级顺序在最新状态上复核（含同批次高优先级者的约束）。
            sibling_cons = Constraints()
            sibling_cons.add(base)
            plans_payload = []
            resv_ids: Dict[str, str] = {}
            for p in planned:
                conflict = validate_path(
                    cur_grid, starts[p.robot_id], p.path, sibling_cons, start_time=0
                )
                if conflict is not None:
                    raise ServiceError(
                        412,
                        "REVALIDATION_CONFLICT",
                        f"提交前复核 {p.robot_id} 的路径时发现冲突，整个批次作废",
                        {"robot_id": p.robot_id, "conflict": conflict},
                    )
                rid = uuid.uuid4().hex
                resv_ids[p.robot_id] = rid
                sibling_cons.add(
                    constraints_from_plans([(p.robot_id, p.path)], 0)
                )

            for p in planned:
                dbmod.insert_reservation(
                    conn,
                    resv_id=resv_ids[p.robot_id],
                    robot_id=p.robot_id,
                    path=p.path,
                    start_time=0,
                    arrival_time=p.arrival_time,
                    map_version=cur_map_version,
                )
            new_version = dbmod.bump_reservation_version(conn, by=len(planned))

            for p in planned:
                rid = resv_ids[p.robot_id]
                token = issue_token(secret, {"resv_id": rid, "robot_id": p.robot_id})
                plans_payload.append(
                    _plan_payload(
                        rid, p.robot_id, p.path, 0, cur_map_version, token
                    )
                )
        return {
            "priority_order": ordered_ids,
            "plans": plans_payload,
            "map_version": cur_map_version,
            "reservation_version": new_version,
        }
    except sqlite3.IntegrityError as exc:
        raise ServiceError(
            409,
            "COMMIT_CONFLICT",
            f"提交时数据库层检测到时空占用冲突（并发预约竞争）：{exc}",
        )
    finally:
        conn.close()


# ---------------------------------------------------------------------- 撤销


def cancel_reservation(
    conn_factory: Callable[[], sqlite3.Connection],
    *,
    resv_id: str,
    token: str,
) -> dict:
    """凭 HMAC 令牌撤销预约；成功后该机器人终点腾出，后续请求可重新规划。"""
    conn = conn_factory()
    try:
        secret = dbmod.get_server_secret(conn)
        try:
            payload = verify_token(secret, token)
        except TokenError as exc:
            raise ServiceError(403, "INVALID_TOKEN", str(exc))
        if payload.get("resv_id") != resv_id:
            raise ServiceError(
                403,
                "TOKEN_RESERVATION_MISMATCH",
                "令牌对应的预约与路径中的预约不一致",
                {"token_resv_id": payload.get("resv_id"), "path_resv_id": resv_id},
            )

        row = conn.execute(
            "SELECT robot_id FROM reservations WHERE resv_id=?", (resv_id,)
        ).fetchone()
        if row is None:
            raise ServiceError(404, "RESV_NOT_FOUND", f"预约 {resv_id} 不存在")

        with dbmod.immediate_tx(conn):
            ok = dbmod.cancel_reservation_rows(conn, resv_id, reason="cancelled_by_token")
            if not ok:
                raise ServiceError(
                    409, "RESV_NOT_ACTIVE", f"预约 {resv_id} 已被撤销，不能重复撤销"
                )
            new_version = dbmod.bump_reservation_version(conn)
        return {
            "resv_id": resv_id,
            "robot_id": row["robot_id"],
            "canceled": True,
            "reservation_version": new_version,
        }
    finally:
        conn.close()


# ---------------------------------------------------------------------- 地图


def update_map(
    conn_factory: Callable[[], sqlite3.Connection],
    *,
    width: int,
    height: int,
    obstacles: List[Cell],
    expected_map_version: Optional[int] = None,
) -> dict:
    """替换地图。现有预约保留，但会报告哪些活跃预约在新地图上不再合法（仅供决策）。"""
    if width <= 0 or height <= 0:
        raise ServiceError(400, "BAD_MAP", "地图宽高必须为正整数")
    try:
        grid = GridMap(width, height, [(int(x), int(y)) for x, y in obstacles])
    except ValueError as exc:
        raise ServiceError(400, "BAD_MAP", str(exc))

    conn = conn_factory()
    try:
        with dbmod.immediate_tx(conn):
            _, current = dbmod.load_map(conn)
            if expected_map_version is not None and expected_map_version != current:
                raise ServiceError(
                    412,
                    "MAP_STALE",
                    f"预期地图版本 {expected_map_version} 与当前版本 {current} 不一致",
                    {
                        "expected_map_version": expected_map_version,
                        "current_map_version": current,
                    },
                )
            new_version, changed = dbmod.replace_map(conn, grid)

            # 评估既有活跃预约在新地图上的受影响情况（不自动删除）。
            affected = []
            for r in dbmod.load_active_reservations(conn):
                bad_cells = sorted(
                    {c for c in r["path"] if not grid.passable(c)}
                )
                if bad_cells:
                    affected.append(
                        {
                            "resv_id": r["resv_id"],
                            "robot_id": r["robot_id"],
                            "impassable_cells": [list(c) for c in bad_cells],
                        }
                    )
        return {
            "map_version": new_version,
            "changed": changed,
            "width": width,
            "height": height,
            "obstacles": [[x, y] for x, y in sorted(grid.obstacles)],
            "affected_reservations": affected,
        }
    finally:
        conn.close()


# ---------------------------------------------------------------------- 状态


def get_state(conn_factory: Callable[[], sqlite3.Connection]) -> dict:
    conn = conn_factory()
    try:
        grid, mv = dbmod.load_map(conn)
        robots = dbmod.load_robots(conn)
        active = dbmod.load_active_reservations(conn)
        rv = dbmod.get_reservation_version(conn)
        active_map = {r["robot_id"]: r for r in active}
        return {
            "map": {"version": mv, **grid.to_json()},
            "reservation_version": rv,
            "robots": [
                {
                    "robot_id": rid,
                    "start": [robots[rid][0], robots[rid][1]],
                    "state": "reserved" if rid in active_map else "free",
                    "active_reservation": active_map[rid]["resv_id"]
                    if rid in active_map
                    else None,
                }
                for rid in sorted(robots)
            ],
            "reservations": [
                {
                    "resv_id": r["resv_id"],
                    "robot_id": r["robot_id"],
                    "path": [[x, y] for x, y in r["path"]],
                    "start_time": r["start_time"],
                    "arrival_time": r["arrival_time"],
                    "goal": [r["goal"][0], r["goal"][1]],
                    "map_version": r["map_version"],
                }
                for r in active
            ],
        }
    finally:
        conn.close()


MAX_ROBOTS = 8


def register_robot(
    conn_factory: Callable[[], sqlite3.Connection],
    *,
    robot_id: str,
    start: Cell,
) -> dict:
    """注册新机器人（最多 8 个），或更新一个当前无活跃预约机器人的起点。"""
    start = (int(start[0]), int(start[1]))
    conn = conn_factory()
    try:
        snap = Snapshot(conn)
        if robot_id not in snap.robots and len(snap.robots) >= MAX_ROBOTS:
            raise ServiceError(
                409,
                "TOO_MANY_ROBOTS",
                f"最多支持 {MAX_ROBOTS} 个机器人，当前已有 {len(snap.robots)} 个",
                {"max_robots": MAX_ROBOTS},
            )
        if robot_id in snap.active_by_robot:
            r = snap.active_by_robot[robot_id]
            raise ServiceError(
                409,
                "ROBOT_BUSY",
                f"机器人 {robot_id} 已有活跃预约 {r['resv_id']}，请先撤销再改起点",
                {"resv_id": r["resv_id"]},
            )
        if not snap.grid.passable(start):
            raise ServiceError(
                400,
                "BAD_START",
                f"起点 {start} 是障碍、越界或在当前地图上不可通行",
                {"cell": list(start), "map_version": snap.map_version},
            )
        occupied = [
            rid
            for rid, cell in snap.robots.items()
            if rid != robot_id and cell == start
        ]
        if occupied:
            raise ServiceError(
                409,
                "START_OCCUPIED",
                f"起点 {start} 已被机器人 {occupied} 注册占用",
                {"cell": list(start), "robots": occupied},
            )
        with dbmod.immediate_tx(conn):
            dbmod.set_robot_start(conn, robot_id, start)
        return {"robot_id": robot_id, "start": list(start)}
    finally:
        conn.close()


def reset(conn_factory: Callable[[], sqlite3.Connection]) -> dict:
    conn = conn_factory()
    try:
        with dbmod.immediate_tx(conn):
            dbmod.reset_to_default(conn)
        return {"status": "reset", **get_state(conn_factory)}
    finally:
        conn.close()
