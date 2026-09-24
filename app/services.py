"""领域服务：匹配、预占、取消、充电点失效、遥测 —— 全部事务化。"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass

from . import config, crypto, energy as energy_mod
from .database import immediate_tx, utcnow

ALERT_KIND_RESERVATION_RELEASED = "reservation_released_recompute"
ALERT_KIND_RUNNING_AT_RISK = "running_mission_charger_failed_risk"
ALERT_KIND_BATTERY_LOWER = "battery_lower_than_predicted"


class ServiceError(Exception):
    def __init__(self, status_code: int, detail: str):
        self.status_code = status_code
        self.detail = detail
        super().__init__(detail)


# ---------------------------------------------------------------- 基础查询

def _active_reserved(conn: sqlite3.Connection, robot_id: str) -> float:
    row = conn.execute(
        "SELECT COALESCE(SUM(reserved_energy_wh),0) AS r "
        "FROM allocations WHERE robot_id=? AND status IN ('planned','running')",
        (robot_id,),
    ).fetchone()
    return float(row["r"])


def _free_chargers(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """可用且未被活跃分配预占的充电点。"""
    return list(conn.execute(
        """
        SELECT c.* FROM chargers c
        WHERE c.status='available'
          AND NOT EXISTS (
              SELECT 1 FROM allocations a
              WHERE a.charger_id=c.id
                AND a.status IN ('planned','running'))
        ORDER BY c.id
        """
    ))


def _add_alert(
    conn: sqlite3.Connection,
    *,
    kind: str,
    severity: str,
    message: str,
    robot_id: str | None = None,
    task_id: str | None = None,
    charger_id: str | None = None,
    data: dict | None = None,
) -> int:
    cur = conn.execute(
        """INSERT INTO alerts
           (kind, severity, robot_id, task_id, charger_id, message,
            data_json, created_at)
           VALUES (?,?,?,?,?,?,?,?)""",
        (kind, severity, robot_id, task_id, charger_id, message,
         json.dumps(data, ensure_ascii=False) if data else None, utcnow()),
    )
    return int(cur.lastrowid)


# ---------------------------------------------------------------- 能量边

@dataclass
class Edge:
    task_id: str
    robot_id: str
    charger_id: str | None
    feasible: bool
    reason: str | None
    available_battery_wh: float
    mission: energy_mod.MissionEnergy | None
    note: str | None = None  # 例如该机器人正忙 / 充电点被他单抢走后重算
    selected: bool = False

    def as_api(self) -> dict:
        return {
            "task_id": self.task_id,
            "robot_id": self.robot_id,
            "charger_id": self.charger_id,
            "feasible": self.feasible,
            "reason": self.reason,
            "available_battery_wh": round(self.available_battery_wh, 6),
            "residual_after_mission_wh": (
                round(
                    energy_mod.residual_after_mission(
                        self.available_battery_wh, self.mission
                    ),
                    6,
                )
                if self.mission is not None
                else None
            ),
            "note": self.note,
            "energy": self.mission.as_dict() if self.mission else None,
            "selected": self.selected,
        }


def _best_edge(
    task: sqlite3.Row,
    robot: sqlite3.Row,
    free_chargers: list[sqlite3.Row],
    margin: float,
    *,
    reason_if_infeasible: str = "insufficient_battery_after_safety_margin",
) -> Edge:
    """为一个 (任务, 机器人) 选择返航能耗最小的可用充电点并判定可达性。"""
    available = float(robot["battery_wh"])
    if not free_chargers:
        return Edge(task["id"], robot["id"], None, False,
                    "no_available_charger", available, None)

    best: sqlite3.Row | None = None
    best_mission: energy_mod.MissionEnergy | None = None
    for ch in free_chargers:
        m = energy_mod.mission_energy(
            robot["x"], robot["y"], task["x"], task["y"],
            ch["x"], ch["y"], task["payload_kg"], task["wait_s"],
        )
        if best_mission is None or (
            m.return_energy_wh, m.total_energy_wh
        ) < (best_mission.return_energy_wh, best_mission.total_energy_wh):
            best, best_mission = ch, m

    feasible = energy_mod.is_reachable(available, best_mission, margin)
    return Edge(
        task["id"], robot["id"], best["id"], feasible,
        None if feasible else reason_if_infeasible,
        available, best_mission,
    )


def _simulated_command(task, robot, charger, mission: energy_mod.MissionEnergy):
    """合成控制指令：明确标注 simulated，系统不会把它发给任何真实设备。"""
    return {
        "simulated": True,
        "do_not_send_to_real_robot": True,
        "type": "goto_mission_then_charge",
        "steps": [
            {"seq": 1, "action": "goto", "target": "task",
             "task_id": task["id"], "x": task["x"], "y": task["y"],
             "distance_m": round(mission.distance_out_m, 6),
             "payload_kg": task["payload_kg"]},
            {"seq": 2, "action": "wait", "wait_s": task["wait_s"]},
            {"seq": 3, "action": "goto", "target": "charger",
             "charger_id": charger["id"], "x": charger["x"], "y": charger["y"],
             "distance_m": round(mission.distance_return_m, 6)},
        ],
    }


# ---------------------------------------------------------------- 分配

ALGORITHM_NAME = "greedy-minimum-cost-feasible-matching/v1"


def dispatch(
    conn: sqlite3.Connection,
    task_ids: list[str],
    margin: float | None = None,
) -> dict:
    """在单个 IMMEDIATE 事务内完成：选边 → 最小成本匹配 → 电量与充电点预占。

    匹配规则（可解释）：在全部「可达」候选边中按
    (总能耗, 任务id, 机器人id) 升序依次取边，任务/机器人/充电点被占用即跳过，
    直到没有可取的边。充电点若在本批次内被更便宜的边先占用，则就剩余充电点
    重算（note 中说明）。不可行边（含「局部最便宜但无法返航」）永远不会被取。
    """
    margin = (
        config.DEFAULT_SAFETY_MARGIN_WH if margin is None else float(margin)
    )
    with immediate_tx(conn):
        tasks = []
        for tid in dict.fromkeys(task_ids):  # 去重保序
            t = conn.execute(
                "SELECT * FROM tasks WHERE id=?", (tid,)
            ).fetchone()
            if t is None:
                raise ServiceError(404, f"task not found: {tid}")
            if t["status"] != "pending":
                raise ServiceError(
                    409,
                    f"task {tid} is not pending (status={t['status']})",
                )
            tasks.append(t)

        free_chargers = _free_chargers(conn)
        chargers_by_id = {c["id"]: c for c in free_chargers}

        # 仅有「无活跃分配」的机器人才可接单（数据库唯一索引也会兜底）
        robots = list(conn.execute(
            """SELECT r.* FROM robots r
               WHERE NOT EXISTS (
                   SELECT 1 FROM allocations a
                   WHERE a.robot_id=r.id
                     AND a.status IN ('planned','running'))
               ORDER BY r.id"""
        ))
        busy_robots = list(conn.execute(
            """SELECT r.* FROM robots r
               WHERE EXISTS (
                   SELECT 1 FROM allocations a
                   WHERE a.robot_id=r.id
                     AND a.status IN ('planned','running'))
               ORDER BY r.id"""
        ))

        edges: list[Edge] = []
        feasible: list[Edge] = []
        for t in tasks:
            for r in busy_robots:
                edges.append(Edge(
                    t["id"], r["id"], None, False, "robot_busy",
                    float(r["battery_wh"]), None,
                ))
            for r in robots:
                e = _best_edge(t, r, free_chargers, margin)
                edges.append(e)
                if e.feasible:
                    feasible.append(e)

        feasible.sort(
            key=lambda e: (
                e.mission.total_energy_wh, e.task_id, e.robot_id
            )
        )

        taken_tasks: set[str] = set()
        taken_robots: set[str] = set()
        claimed_chargers: set[str] = set()
        assignments: list[tuple[Edge, object]] = []

        for e in feasible:
            if e.task_id in taken_tasks or e.robot_id in taken_robots:
                continue
            edge_charger = e.charger_id
            note = None
            mission = e.mission
            if edge_charger in claimed_chargers:
                # 本批次竞争：静态首选充电点已被更便宜的边抢走，就剩余的重算
                remaining = [
                    c for cid, c in chargers_by_id.items()
                    if cid not in claimed_chargers
                ]
                task_row = next(t for t in tasks if t["id"] == e.task_id)
                robot_row = next(r for r in robots if r["id"] == e.robot_id)
                re_edge = _best_edge(
                    task_row, robot_row, remaining, margin,
                    reason_if_infeasible="all_chargers_claimed_or_unreachable",
                )
                note = (
                    f"preferred charger {edge_charger} claimed earlier in "
                    "this batch; recomputed against remaining chargers"
                )
                if not re_edge.feasible:
                    re_edge.note = note
                    edges.append(re_edge)
                    continue
                e = re_edge
                e.note = note
                edges.append(e)
                mission = e.mission
                edge_charger = e.charger_id

            # —— 落库：任务、电量预占、充电点预占同事务 ——
            t = next(x for x in tasks if x["id"] == e.task_id)
            r = next(x for x in robots if x["id"] == e.robot_id)
            ch = chargers_by_id[edge_charger]
            created_at = utcnow()
            reserved = mission.total_energy_wh + margin
            cur = conn.execute(
                """INSERT INTO allocations
                   (task_id, robot_id, charger_id, distance_out_m,
                    distance_return_m, outbound_energy_wh, wait_energy_wh,
                    return_energy_wh, total_energy_wh, safety_margin_wh,
                    reserved_energy_wh, status, created_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,'planned',?)""",
                (t["id"], r["id"], ch["id"],
                 mission.distance_out_m, mission.distance_return_m,
                 mission.outbound_energy_wh, mission.wait_energy_wh,
                 mission.return_energy_wh, mission.total_energy_wh,
                 margin, reserved, created_at),
            )
            allocation_id = int(cur.lastrowid)
            conn.execute(
                "UPDATE tasks SET status='assigned' WHERE id=?", (t["id"],)
            )
            conn.execute(
                "UPDATE robots SET status='busy' WHERE id=?", (r["id"],)
            )
            receipt = crypto.receipt_payload(
                allocation_id=allocation_id, task_id=t["id"],
                robot_id=r["id"], charger_id=ch["id"],
                outbound_energy_wh=mission.outbound_energy_wh,
                wait_energy_wh=mission.wait_energy_wh,
                return_energy_wh=mission.return_energy_wh,
                total_energy_wh=mission.total_energy_wh,
                safety_margin_wh=margin, created_at=created_at,
            )
            cmd = _simulated_command(t, r, ch, mission)
            assignments.append((e, {
                "task_id": t["id"],
                "robot_id": r["id"],
                "charger_id": ch["id"],
                "allocation_id": allocation_id,
                "energy": mission.as_dict(),
                "reserved_energy_wh": round(reserved, 6),
                "safety_margin_wh": round(margin, 6),
                "receipt": receipt,
                "simulated_command": cmd,
            }))

            # 标记静态边（以及重算边）被选中
            for ee in edges:
                if (
                    ee.task_id == e.task_id
                    and ee.robot_id == e.robot_id
                    and ee.charger_id == edge_charger
                ):
                    ee.selected = True

            taken_tasks.add(e.task_id)
            taken_robots.add(e.robot_id)
            claimed_chargers.add(edge_charger)

        assigned_task_ids = {a[1]["task_id"] for a in assignments}
        unassigned = []
        for t in tasks:
            if t["id"] in assigned_task_ids:
                continue
            task_edges = [e for e in edges if e.task_id == t["id"]]
            if not free_chargers:
                reason = "no_available_charger"
            elif any(e.reason == "no_available_charger"
                     for e in task_edges):
                reason = "no_available_charger"
            elif any(e.reason == "all_chargers_claimed_or_unreachable"
                     for e in task_edges):
                reason = "all_chargers_claimed"
            elif not any(e.feasible for e in task_edges):
                reason = "no_feasible_robot"
            else:
                reason = "all_feasible_robots_committed_to_lower_cost_tasks"
            unassigned.append({"task_id": t["id"], "reason": reason})

        return {
            "assigned": [a[1] for a in assignments],
            "unassigned": unassigned,
            "candidate_edges": [e.as_api() for e in edges],
            "algorithm": ALGORITHM_NAME,
        }


# ---------------------------------------------------------------- 执行生命周期

def start_task(conn: sqlite3.Connection, task_id: str) -> dict:
    with immediate_tx(conn):
        a = conn.execute(
            "SELECT * FROM allocations WHERE task_id=? "
            "AND status IN ('planned','running')",
            (task_id,),
        ).fetchone()
        if a is None:
            raise ServiceError(404, f"no active allocation for task {task_id}")
        if a["status"] == "running":
            raise ServiceError(409, f"task {task_id} already running")
        r = conn.execute("SELECT * FROM robots WHERE id=?",
                         (a["robot_id"],)).fetchone()
        conn.execute(
            "UPDATE allocations SET status='running', start_at=?, "
            "start_battery_wh=? WHERE id=?",
            (utcnow(), r["battery_wh"], a["id"]),
        )
        conn.execute("UPDATE tasks SET status='running' WHERE id=?",
                     (task_id,))
        return {"task_id": task_id, "status": "running",
                "start_battery_wh": float(r["battery_wh"])}


def complete_task(conn: sqlite3.Connection, task_id: str,
                  measured_battery_wh: float | None = None) -> dict:
    with immediate_tx(conn):
        a = conn.execute(
            "SELECT * FROM allocations WHERE task_id=? AND status='running'",
            (task_id,),
        ).fetchone()
        if a is None:
            raise ServiceError(409, f"task {task_id} is not running")
        ch = conn.execute("SELECT * FROM chargers WHERE id=?",
                          (a["charger_id"],)).fetchone()
        r = conn.execute("SELECT * FROM robots WHERE id=?",
                         (a["robot_id"],)).fetchone()
        # 结算电量：优先使用实测值，否则按公式预测
        if measured_battery_wh is None:
            final_batt = float(r["battery_wh"]) - float(a["total_energy_wh"])
            source = "formula"
        else:
            final_batt = float(measured_battery_wh)
            source = "measured"
        now = utcnow()
        conn.execute(
            "UPDATE allocations SET status='completed', complete_at=?, "
            "completed_at=? WHERE id=?",
            (now, now, a["id"]),
        )
        conn.execute("UPDATE tasks SET status='completed' WHERE id=?",
                     (task_id,))
        conn.execute(
            "UPDATE robots SET status='idle', x=?, y=?, battery_wh=? "
            "WHERE id=?",
            (ch["x"], ch["y"], max(final_batt, 0.0), r["id"]),
        )
        return {"task_id": task_id, "status": "completed",
                "robot_id": r["id"], "charger_id": ch["id"],
                "battery_wh_source": source,
                "battery_wh": round(max(final_batt, 0.0), 6)}


def cancel_reservation(conn: sqlite3.Connection, task_id: str,
                       reason: str = "cancelled_by_request") -> dict:
    """取消未开始任务的预占：释放电量预占与充电点，任务回到 pending 可重算。"""
    with immediate_tx(conn):
        a = conn.execute(
            "SELECT * FROM allocations WHERE task_id=? "
            "AND status IN ('planned','running')",
            (task_id,),
        ).fetchone()
        if a is None:
            raise ServiceError(404, f"no active allocation for task {task_id}")
        if a["status"] != "planned":
            raise ServiceError(
                409,
                f"task {task_id} already running; cannot cancel reservation",
            )
        now = utcnow()
        conn.execute(
            "UPDATE allocations SET status='cancelled', cancelled_at=? "
            "WHERE id=?",
            (now, a["id"]),
        )
        conn.execute("UPDATE tasks SET status='pending' WHERE id=?",
                     (task_id,))
        conn.execute("UPDATE robots SET status='idle' WHERE id=?",
                     (a["robot_id"],))
        _add_alert(
            conn, kind="reservation_cancelled", severity="warning",
            message=f"reservation for task {task_id} released; "
                    "task returns to pending for recompute",
            robot_id=a["robot_id"], task_id=task_id,
            charger_id=a["charger_id"],
            data={"reason": reason,
                  "released_energy_wh": float(a["reserved_energy_wh"])},
        )
        return {"task_id": task_id, "status": "pending",
                "released_reserved_energy_wh": float(a["reserved_energy_wh"]),
                "released_charger_id": a["charger_id"]}


# ---------------------------------------------------------------- 充电点失效

def fail_charger(conn: sqlite3.Connection, charger_id: str,
                 reason: str = "", auto_recompute: bool = True) -> dict:
    """充电点失效：未开始任务释放并立即重算；运行中任务保留但告警。"""
    with immediate_tx(conn):
        ch = conn.execute("SELECT * FROM chargers WHERE id=?",
                          (charger_id,)).fetchone()
        if ch is None:
            raise ServiceError(404, f"charger not found: {charger_id}")
        if ch["status"] == "failed":
            raise ServiceError(409, f"charger {charger_id} already failed")
        now = utcnow()
        conn.execute(
            "UPDATE chargers SET status='failed', failed_at=?, reason=? "
            "WHERE id=?",
            (now, reason, charger_id),
        )

        planned = list(conn.execute(
            "SELECT * FROM allocations WHERE charger_id=? AND status='planned'",
            (charger_id,),
        ))
        running = list(conn.execute(
            "SELECT * FROM allocations WHERE charger_id=? AND status='running'",
            (charger_id,),
        ))

        released_task_ids: list[str] = []
        for a in planned:
            conn.execute(
                "UPDATE allocations SET status='cancelled', cancelled_at=? "
                "WHERE id=?",
                (now, a["id"]),
            )
            conn.execute("UPDATE tasks SET status='pending' WHERE id=?",
                         (a["task_id"],))
            conn.execute("UPDATE robots SET status='idle' WHERE id=?",
                         (a["robot_id"],))
            released_task_ids.append(a["task_id"])
            _add_alert(
                conn, kind=ALERT_KIND_RESERVATION_RELEASED, severity="warning",
                message=f"charger {charger_id} failed before task "
                        f"{a['task_id']} started; reservation released "
                        "and queued for recompute",
                robot_id=a["robot_id"], task_id=a["task_id"],
                charger_id=charger_id,
                data={"released_energy_wh": float(a["reserved_energy_wh"])},
            )

        at_risk = []
        for a in running:
            r = conn.execute("SELECT * FROM robots WHERE id=?",
                             (a["robot_id"],)).fetchone()
            t = conn.execute("SELECT * FROM tasks WHERE id=?",
                             (a["task_id"],)).fetchone()
            # 预测其到失效充电点后的剩余（公式值，真实执行轨迹未知 -> 告警）
            predicted_residual = (
                (float(a["start_battery_wh"] or r["battery_wh"]))
                - float(a["total_energy_wh"])
            )
            _add_alert(
                conn, kind=ALERT_KIND_RUNNING_AT_RISK, severity="critical",
                message=f"task {a['task_id']} is already running toward "
                        f"failed charger {charger_id}; mission kept, "
                        "diversion to another charger required",
                robot_id=a["robot_id"], task_id=a["task_id"],
                charger_id=charger_id,
                data={"predicted_residual_wh": predicted_residual,
                      "safety_margin_wh": float(a["safety_margin_wh"]),
                      "task_x": t["x"], "task_y": t["y"]},
            )
            at_risk.append({"task_id": a["task_id"],
                            "robot_id": a["robot_id"],
                            "predicted_residual_wh": predicted_residual})

    recomputed = None
    if auto_recompute and released_task_ids:
        # 用新的可用充电点集合在独立事务中重新分配
        recomputed = dispatch(
            conn, released_task_ids,
            margin=None,  # 沿用各任务原 margin？新分配用系统默认余量
        )
    return {
        "charger_id": charger_id,
        "status": "failed",
        "released_for_recompute": released_task_ids,
        "running_missions_kept_at_risk": at_risk,
        "recompute": recomputed,
    }


# ---------------------------------------------------------------- 遥测

def _consumed_along_path(allocation: sqlite3.Row,
                         completed_distance_m: float) -> float:
    """按路径分段（满载去程 / 空载返航）计算已发生能耗。"""
    d_out = float(allocation["distance_out_m"])
    d = min(completed_distance_m,
             d_out + float(allocation["distance_return_m"]))
    if d <= d_out:
        # 用去程预算的单位能耗（已含载荷），保持与预占公式一致
        rate_out = (
            float(allocation["outbound_energy_wh"]) / d_out if d_out else 0.0
        )
        return rate_out * d
    rate_ret = (
        float(allocation["return_energy_wh"])
        / float(allocation["distance_return_m"])
        if allocation["distance_return_m"] else 0.0
    )
    return float(allocation["outbound_energy_wh"]) + rate_ret * (d - d_out)


def report_telemetry(conn: sqlite3.Connection, robot_id: str,
                     battery_wh: float, x: float | None,
                     y: float | None, completed_distance_m: float | None
                     ) -> dict:
    with immediate_tx(conn):
        r = conn.execute("SELECT * FROM robots WHERE id=?",
                         (robot_id,)).fetchone()
        if r is None:
            raise ServiceError(404, f"robot not found: {robot_id}")
        now = utcnow()
        conn.execute(
            "UPDATE robots SET battery_wh=?, "
            "x=COALESCE(?, x), y=COALESCE(?, y), updated_at=? WHERE id=?",
            (battery_wh, x, y, now, robot_id),
        )

        predicted_remaining = None
        alerts_out: list[dict] = []
        a = conn.execute(
            "SELECT * FROM allocations WHERE robot_id=? "
            "AND status='running'",
            (robot_id,),
        ).fetchone()
        if a is not None and completed_distance_m is not None:
            consumed = _consumed_along_path(a, completed_distance_m)
            start_batt = float(
                a["start_battery_wh"]
                if a["start_battery_wh"] is not None else battery_wh + consumed
            )
            predicted_remaining = start_batt - consumed
            conn.execute(
                """INSERT INTO telemetry
                   (robot_id, battery_wh, x, y,
                    predicted_remaining_wh, recorded_at)
                   VALUES (?,?,?,?,?,?)""",
                (robot_id, battery_wh, x, y, predicted_remaining, now),
            )
            tol = max(0.5, config.MEASUREMENT_TOLERANCE
                      * max(predicted_remaining, 0.0))
            if battery_wh < predicted_remaining - tol:
                deficit = predicted_remaining - battery_wh
                aid = _add_alert(
                    conn, kind=ALERT_KIND_BATTERY_LOWER, severity="critical",
                    message=f"robot {robot_id} measured battery "
                            f"{battery_wh:.3f}Wh is below predicted "
                            f"{predicted_remaining:.3f}Wh by "
                            f"{deficit:.3f}Wh on task {a['task_id']}",
                    robot_id=robot_id, task_id=a["task_id"],
                    charger_id=a["charger_id"],
                    data={"measured_wh": battery_wh,
                          "predicted_remaining_wh": predicted_remaining,
                          "deficit_wh": deficit,
                          "completed_distance_m": completed_distance_m,
                          "reserved_total_wh": float(a["total_energy_wh"]),
                          "safety_margin_wh": float(a["safety_margin_wh"])},
                )
                alerts_out.append({"id": aid, "deficit_wh": deficit})
        else:
            conn.execute(
                """INSERT INTO telemetry
                   (robot_id, battery_wh, x, y,
                    predicted_remaining_wh, recorded_at)
                   VALUES (?,?,?,?,NULL,?)""",
                (robot_id, battery_wh, x, y, now),
            )

        return {
            "robot_id": robot_id, "battery_wh": battery_wh,
            "predicted_remaining_wh": (
                round(predicted_remaining, 6)
                if predicted_remaining is not None else None
            ),
            "alerts": alerts_out,
        }
