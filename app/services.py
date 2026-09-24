"""Transactional business logic for task allocation."""
from __future__ import annotations

import json
import threading
import time
import uuid
from dataclasses import asdict
from typing import Any

from . import config, crypto
from .database import connect, transaction
from .energy import ChargerOption, MissionPlan, plan_mission
from .matching import hungarian

# All state-mutating flows are serialised. SQLite already serialises writers
# via BEGIN IMMEDIATE, but charger-slot contention planning spans reads inside
# one allocation transaction, and a process-level lock keeps the in-transaction
# reservation view honest.
WRITE_LOCK = threading.RLock()

ACTIVE_STATES = ("reserved", "running", "charging")


# ---------------------------------------------------------------------------
# row serialisation
# ---------------------------------------------------------------------------

def _robot(r) -> dict[str, Any]:
    return {
        "id": r["id"], "name": r["name"],
        "position": [r["pos_x"], r["pos_y"]],
        "current_soc_kwh": r["current_soc_kwh"],
        "capacity_kwh": r["capacity_kwh"],
        "payload_capacity_kg": r["payload_capacity_kg"],
        "status": r["status"], "at_risk": bool(r["at_risk"]),
    }


def _charger(r) -> dict[str, Any]:
    return {
        "id": r["id"], "name": r["name"],
        "position": [r["pos_x"], r["pos_y"]],
        "capacity": r["capacity"], "status": r["status"],
    }


def _task(r) -> dict[str, Any]:
    return {
        "id": r["id"], "title": r["title"],
        "pickup": [r["pickup_x"], r["pickup_y"]],
        "delivery": [r["delivery_x"], r["delivery_y"]],
        "payload_kg": r["payload_kg"], "wait_seconds": r["wait_seconds"],
        "priority": r["priority"], "status": r["status"],
    }


def _assignment(r) -> dict[str, Any]:
    return {
        "id": r["id"], "task_id": r["task_id"], "robot_id": r["robot_id"],
        "charger_id": r["charger_id"], "status": r["status"],
        "token": r["token"],
        "predicted_soc_kwh": r["predicted_soc_kwh"],
        "required_soc_kwh": r["required_soc_kwh"],
        "reserve_eta_m": r["reserve_eta_m"],
        "created_at": r["created_at"], "started_at": r["started_at"],
        "completed_at": r["completed_at"],
        "explanation": json.loads(r["explanation_json"]),
    }


def _alert(r) -> dict[str, Any]:
    return {
        "id": r["id"], "kind": r["kind"], "severity": r["severity"],
        "entity_type": r["entity_type"], "entity_id": r["entity_id"],
        "message": r["message"],
        "detail": json.loads(r["detail_json"]) if r["detail_json"] else None,
        "acknowledged": bool(r["acknowledged"]), "created_at": r["created_at"],
    }


# ---------------------------------------------------------------------------
# CRUD
# ---------------------------------------------------------------------------

def create_operator(conn, username: str, password: str) -> int:
    cur = conn.execute(
        "INSERT INTO operators(username, password_hash) VALUES (?, ?)",
        (username, crypto.hash_password(password)),
    )
    return cur.lastrowid


def get_operator_by_username(conn, username: str):
    return conn.execute(
        "SELECT * FROM operators WHERE username = ?", (username,)
    ).fetchone()


def create_robot(data) -> dict:
    with WRITE_LOCK:
        conn = connect()
        try:
            with transaction(conn):
                conn.execute(
                    """INSERT INTO robots(id,name,pos_x,pos_y,current_soc_kwh,
                       capacity_kwh,payload_capacity_kg)
                       VALUES (?,?,?,?,?,?,?)""",
                    (data.id, data.name, *data.position, data.current_soc_kwh,
                     data.capacity_kwh, data.payload_capacity_kg),
                )
                row = conn.execute("SELECT * FROM robots WHERE id=?",
                                   (data.id,)).fetchone()
                return _robot(row)
        finally:
            conn.close()


def list_robots() -> list[dict]:
    conn = connect()
    try:
        return [_robot(r) for r in conn.execute("SELECT * FROM robots ORDER BY id")]
    finally:
        conn.close()


def get_robot(robot_id: str):
    conn = connect()
    try:
        row = conn.execute("SELECT * FROM robots WHERE id=?",
                           (robot_id,)).fetchone()
        return _robot(row) if row else None
    finally:
        conn.close()


def create_charger(data) -> dict:
    with WRITE_LOCK:
        conn = connect()
        try:
            with transaction(conn):
                conn.execute(
                    "INSERT INTO chargers(id,name,pos_x,pos_y,capacity)"
                    " VALUES (?,?,?,?,?)",
                    (data.id, data.name, *data.position, data.capacity),
                )
                row = conn.execute("SELECT * FROM chargers WHERE id=?",
                                   (data.id,)).fetchone()
                return _charger(row)
        finally:
            conn.close()


def list_chargers() -> list[dict]:
    conn = connect()
    try:
        return [_charger(c) for c in
                conn.execute("SELECT * FROM chargers ORDER BY id")]
    finally:
        conn.close()


def create_task(data) -> dict:
    with WRITE_LOCK:
        conn = connect()
        try:
            with transaction(conn):
                conn.execute(
                    """INSERT INTO tasks(id,title,pickup_x,pickup_y,
                       delivery_x,delivery_y,payload_kg,wait_seconds,priority)
                       VALUES (?,?,?,?,?,?,?,?,?)""",
                    (data.id, data.title, *data.pickup, *data.delivery,
                     data.payload_kg, data.wait_seconds, data.priority),
                )
                row = conn.execute("SELECT * FROM tasks WHERE id=?",
                                   (data.id,)).fetchone()
                return _task(row)
        finally:
            conn.close()


def list_tasks() -> list[dict]:
    conn = connect()
    try:
        return [_task(t) for t in conn.execute("SELECT * FROM tasks ORDER BY id")]
    finally:
        conn.close()


def list_assignments(status: str | None = None) -> list[dict]:
    conn = connect()
    try:
        if status:
            rows = conn.execute(
                "SELECT * FROM assignments WHERE status=? ORDER BY created_at",
                (status,))
        else:
            rows = conn.execute(
                "SELECT * FROM assignments ORDER BY created_at DESC")
        return [_assignment(r) for r in rows]
    finally:
        conn.close()


def list_alerts(severity: str | None = None) -> list[dict]:
    conn = connect()
    try:
        sql = "SELECT * FROM alerts"
        args: tuple = ()
        if severity:
            sql += " WHERE severity=?"
            args = (severity,)
        sql += " ORDER BY id DESC"
        return [_alert(r) for r in conn.execute(sql, args)]
    finally:
        conn.close()


def list_dispatch_logs() -> list[dict]:
    conn = connect()
    try:
        rows = conn.execute(
            "SELECT * FROM dispatch_logs ORDER BY id DESC LIMIT 200").fetchall()
        return [
            {"id": r["id"], "assignment_id": r["assignment_id"],
             "action": r["action"],
             "payload": json.loads(r["payload_json"]) if r["payload_json"] else None,
             "created_at": r["created_at"], "simulated": True}
            for r in rows
        ]
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# explainability
# ---------------------------------------------------------------------------

def _charger_option_dict(o: ChargerOption) -> dict:
    return {
        "charger_id": o.charger_id,
        "charger_position": list(o.charger_pos),
        "return_m": round(o.return_m, 4),
        "return_energy_kwh": round(o.return_energy_kwh, 6),
        "required_soc_kwh": round(o.required_soc_kwh, 6),
        "reserve_eta_proxy_m": round(o.reserve_eta_s, 4),
        "safety_margin_kwh": round(o.safety_margin_kwh, 6),
    }


def explain_plan(plan: MissionPlan, chosen_charger_id: str | None = None) -> dict:
    data = asdict(plan)
    data.pop("best_charger", None)
    data.pop("charger_options", None)
    for key in ("outbound_m", "laden_m", "return_m", "total_distance_m"):
        data[key] = round(data[key], 4)
    for key in ("outbound_energy_kwh", "laden_energy_kwh", "wait_energy_kwh",
                "return_energy_kwh", "mission_energy_kwh", "safety_margin_kwh",
                "required_soc_kwh", "available_soc_kwh", "energy_cost",
                "distance_cost", "wait_cost", "priority_cost"):
        data[key] = round(data[key], 6) if data[key] != float("inf") else None
    data["total_cost"] = (round(plan.total_cost, 6)
                          if plan.total_cost != float("inf") else None)
    data["charger_options"] = [
        _charger_option_dict(o) for o in plan.charger_options
    ]
    data["chosen_charger_id"] = chosen_charger_id
    data["cost_breakdown"] = {
        "energy_cost": data["energy_cost"],
        "distance_cost": data["distance_cost"],
        "wait_cost": data["wait_cost"],
        "priority_cost": data["priority_cost"],
    }
    return data


def preview_cost(data) -> dict:
    conn = connect()
    try:
        chargers = [
            (c["id"], (c["pos_x"], c["pos_y"]))
            for c in conn.execute(
                "SELECT * FROM chargers WHERE status='available'")
        ]
    finally:
        conn.close()
    plan = plan_mission(
        robot_id="(preview)", robot_pos=tuple(data.robot_pos),
        current_soc_kwh=data.current_soc_kwh, capacity_kwh=data.capacity_kwh,
        task_id="(preview)", pickup_pos=tuple(data.pickup),
        delivery_pos=tuple(data.delivery), payload_kg=data.payload_kg,
        wait_seconds=data.wait_seconds, available_chargers=chargers,
        priority=data.priority,
    )
    out = explain_plan(plan)
    out["simulated"] = True
    return out


# ---------------------------------------------------------------------------
# allocation
# ---------------------------------------------------------------------------

def _pair_plan(robot, task, chargers) -> MissionPlan:
    return plan_mission(
        robot_id=robot["id"],
        robot_pos=(robot["pos_x"], robot["pos_y"]),
        current_soc_kwh=robot["current_soc_kwh"],
        capacity_kwh=robot["capacity_kwh"],
        task_id=task["id"],
        pickup_pos=(task["pickup_x"], task["pickup_y"]),
        delivery_pos=(task["delivery_x"], task["delivery_y"]),
        payload_kg=task["payload_kg"],
        wait_seconds=task["wait_seconds"],
        available_chargers=[
            (c["id"], (c["pos_x"], c["pos_y"])) for c in chargers
        ],
        priority=task["priority"],
    )


def _pair_payload_ok(robot, task) -> bool:
    cap = robot["payload_capacity_kg"] or 0.0
    return cap <= 0 or task["payload_kg"] <= cap


def _charger_reservation_counts(conn) -> dict[str, int]:
    counts: dict[str, int] = {}
    for r in conn.execute(
        """SELECT charger_id, COUNT(*) AS n FROM assignments
           WHERE status IN ('reserved','running','charging')
           GROUP BY charger_id"""
    ):
        counts[r["charger_id"]] = r["n"]
    return counts


def _pick_charger(plan: MissionPlan, charger_caps: dict[str, int],
                  used: dict[str, int]) -> ChargerOption | None:
    """First feasible charger option (nearest first) with a free slot."""
    for opt in plan.charger_options:
        cid = opt.charger_id
        if used.get(cid, 0) < charger_caps.get(cid, 0) \
                and plan.available_soc_kwh >= opt.required_soc_kwh:
            return opt
    return None


def _per_robot_entries(task, robots, busy_robots, plans, blocked) -> list[dict]:
    """Per-robot feasibility/cost explanation rows for one task."""
    entries: list[dict] = []
    for rb in robots:
        plan = plans.get((rb["id"], task["id"]))
        entry: dict[str, Any] = {
            "robot_id": rb["id"],
            "feasible": bool(plan and plan.feasible
                             and _pair_payload_ok(rb, task)
                             and (rb["id"], task["id"]) not in blocked),
            "reason": "blocked_by_charger_contention"
            if (rb["id"], task["id"]) in blocked else (
                "payload_over_capacity"
                if not _pair_payload_ok(rb, task)
                else (plan.reason if plan else "unknown")),
        }
        if plan:
            entry.update({
                "mission_energy_kwh": round(plan.mission_energy_kwh, 6),
                "required_soc_kwh":
                    round(plan.required_soc_kwh, 6)
                    if plan.required_soc_kwh != float("inf") else None,
                "available_soc_kwh": round(plan.available_soc_kwh, 6),
                "total_distance_m": round(plan.total_distance_m, 4),
                "total_cost": round(plan.total_cost, 6)
                if plan.total_cost != float("inf") else None,
                "nearest_charger_id":
                    plan.charger_options[0].charger_id
                    if plan.charger_options else None,
            })
        entries.append(entry)
    for rb in busy_robots:
        entries.append({
            "robot_id": rb["id"], "feasible": False,
            "reason": "robot_not_idle",
            "robot_status": rb["status"],
        })
    return entries


def _allocate(conn, only_task_ids: set[str] | None = None) -> dict[str, Any]:
    """Run global minimum-cost matching and commit atomically.

    Caller must hold WRITE_LOCK and a transaction is opened here.
    """
    with transaction(conn):
        robots = conn.execute(
            "SELECT * FROM robots WHERE status='idle' AND at_risk=0"
        ).fetchall()
        task_sql = "SELECT * FROM tasks WHERE status='pending'"
        args: list = []
        if only_task_ids is not None:
            if not only_task_ids:
                return {"assignments": [], "unassigned": [], "blocked": [],
                        "simulated": True}
            marks = ",".join("?" * len(only_task_ids))
            task_sql += f" AND id IN ({marks})"
            args = list(only_task_ids)
        tasks = conn.execute(task_sql, args).fetchall()
        chargers = conn.execute(
            "SELECT * FROM chargers WHERE status='available'").fetchall()
        charger_caps = {c["id"]: c["capacity"] for c in chargers}

        # Busy robots are included in the explanation but not the matrix.
        busy_robots = conn.execute(
            "SELECT * FROM robots WHERE status != 'idle'").fetchall()
        busy_of = {r["id"]: r for r in busy_robots}

        # Cache plans per pair; inputs are fixed for the transaction.
        plans: dict[tuple[str, str], MissionPlan] = {}
        blocked: set[tuple[str, str]] = set()
        matched: list[tuple[Any, Any, MissionPlan, ChargerOption]] = []

        if robots and tasks:
            for rb in robots:
                for t in tasks:
                    plans[(rb["id"], t["id"])] = _pair_plan(rb, t, chargers)

            # Re-run Hungarian while charger contention forces pairs out.
            # Bounded by (#robots * #tasks) rounds.
            for _round in range(len(robots) * len(tasks) + 1):
                matrix = []
                for rb in robots:
                    row = []
                    for t in tasks:
                        plan = plans[(rb["id"], t["id"])]
                        inf_pair = (
                            (rb["id"], t["id"]) in blocked
                            or not plan.feasible
                            or not _pair_payload_ok(rb, t)
                        )
                        row.append(float("inf") if inf_pair
                                   else plan.total_cost)
                    matrix.append(row)

                _, assignment = hungarian(matrix)
                candidate = []
                for ri, t_idx in enumerate(assignment):
                    if t_idx == -1:
                        continue
                    rb, t = robots[ri], tasks[t_idx]
                    candidate.append((rb, t, plans[(rb["id"], t["id"])]))

                # Resolve charger slots for the matched set in ETA order:
                # the robot that would arrive earliest claims the slot.
                candidate.sort(key=lambda x: (
                    x[2].best_charger.reserve_eta_s if x[2].best_charger
                    else float("inf"), x[2].total_cost))
                used = _charger_reservation_counts(conn)
                resolved: list[tuple[Any, Any, MissionPlan, ChargerOption]] = []
                losers: list[tuple[str, str]] = []
                for rb, t, plan in candidate:
                    opt = _pick_charger(plan, charger_caps, used)
                    if opt is None:
                        losers.append((rb["id"], t["id"]))
                        continue
                    used[opt.charger_id] = used.get(opt.charger_id, 0) + 1
                    resolved.append((rb, t, plan, opt))

                if not losers:
                    matched = resolved
                    break

                progressed = False
                for rid, tid in losers:
                    if (rid, tid) not in blocked:
                        blocked.add((rid, tid))
                        progressed = True
                if not progressed:
                    # Every losing pair already blocked: emit what resolved.
                    matched = resolved
                    break
            else:  # pragma: no cover - loop bound is defensive
                raise RuntimeError("allocation did not converge")

        assignments_out: list[dict] = []
        assigned_task_ids: set[str] = set()
        assigned_robot_ids: set[str] = set()
        now = int(time.time())

        # Commit reservations deterministically (earliest ETA first).
        commit_order = sorted(matched, key=lambda x: x[3].reserve_eta_s)

        for item in commit_order:
            rb, t, plan, opt = item
            aid = "asn-" + uuid.uuid4().hex[:16]
            token = crypto.issue_token(
                {"sub": aid, "kind": "assignment", "robot": rb["id"],
                 "task": t["id"], "charger": opt.charger_id},
                ttl_seconds=24 * 3600,
            )
            explanation = explain_plan(plan, opt.charger_id)
            explanation["dispatch_ts"] = now
            conn.execute(
                """INSERT INTO assignments(id,task_id,robot_id,charger_id,
                   status,token,explanation_json,predicted_soc_kwh,
                   required_soc_kwh,reserve_eta_m)
                   VALUES (?,?,?,?,'reserved',?,?,?,?,?)""",
                (aid, t["id"], rb["id"], opt.charger_id, token,
                 json.dumps(explanation), rb["current_soc_kwh"],
                 opt.required_soc_kwh, opt.reserve_eta_s),
            )
            conn.execute(
                "UPDATE robots SET status='reserved' WHERE id=?", (rb["id"],))
            conn.execute(
                "UPDATE tasks SET status='assigned' WHERE id=?", (t["id"],))
            conn.execute(
                """INSERT INTO dispatch_logs(assignment_id,action,payload_json)
                   VALUES (?,?,?)""",
                (aid, "dispatch_simulated", json.dumps({
                    "simulated": True,
                    "note": "No real control command emitted: planning demo.",
                    "robot": rb["id"], "task": t["id"],
                    "charger": opt.charger_id,
                    "required_soc_kwh": round(opt.required_soc_kwh, 6),
                })),
            )
            row = conn.execute("SELECT * FROM assignments WHERE id=?",
                               (aid,)).fetchone()
            assignments_out.append(_assignment(row))
            assigned_task_ids.add(t["id"])
            assigned_robot_ids.add(rb["id"])

        # Explain why each remaining pending task was not assigned.
        unassigned: list[dict] = []
        for t in tasks:
            if t["id"] in assigned_task_ids:
                continue
            per_robot = _per_robot_entries(t, robots, busy_robots, plans,
                                           blocked)
            any_contention = any(
                e["reason"] == "blocked_by_charger_contention"
                for e in per_robot)
            unassigned.append({
                "task_id": t["id"],
                "reason": "charger_contention" if any_contention
                else ("no_idle_robot" if not robots
                      else "no_feasible_robot"),
                "per_robot": per_robot,
            })

        # Attach rejected alternatives to each committed assignment so the
        # operator can see *why* other robots lost (e.g. locally cheapest but
        # unable to reach a charger).
        task_by_id = {t["id"]: t for t in tasks}
        for out in assignments_out:
            t = task_by_id[out["task_id"]]
            out["rejected_alternatives"] = [
                e for e in _per_robot_entries(t, robots, busy_robots, plans,
                                              blocked)
                if e["robot_id"] != out["robot_id"]
            ]

        return {
            "assignments": assignments_out,
            "unassigned": unassigned,
            "blocked": sorted(list(blocked)),
            "simulated": True,
        }


def batch_allocate(only_task_ids: set[str] | None = None) -> dict[str, Any]:
    with WRITE_LOCK:
        conn = connect()
        try:
            return _allocate(conn, only_task_ids=only_task_ids)
        finally:
            conn.close()


# ---------------------------------------------------------------------------
# assignment lifecycle
# ---------------------------------------------------------------------------

def _authenticate_assignment_token(conn, assignment_id: str,
                                   token: str) -> dict:
    from fastapi import HTTPException
    try:
        claims = crypto.verify_token(token)
    except crypto.TokenError as exc:
        raise HTTPException(status_code=401,
                            detail=f"invalid assignment token: {exc}")
    if claims.get("sub") != assignment_id or claims.get("kind") != "assignment":
        raise HTTPException(status_code=403,
                            detail="token does not match assignment")
    row = conn.execute("SELECT * FROM assignments WHERE id=?",
                       (assignment_id,)).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="assignment not found")
    return row


def start_assignment(assignment_id: str, token: str) -> dict:
    with WRITE_LOCK:
        conn = connect()
        try:
            with transaction(conn):
                row = _authenticate_assignment_token(conn, assignment_id, token)
                if row["status"] != "reserved":
                    from fastapi import HTTPException
                    raise HTTPException(
                        status_code=409,
                        detail=f"cannot start from status {row['status']}")
                robot = conn.execute("SELECT * FROM robots WHERE id=?",
                                     (row["robot_id"],)).fetchone()
                latest = conn.execute(
                    """SELECT * FROM telemetry WHERE assignment_id=?
                       ORDER BY id DESC LIMIT 1""",
                    (assignment_id,)).fetchone()
                if robot["at_risk"] or (latest and latest["severity"] == "critical"):
                    from fastapi import HTTPException
                    raise HTTPException(
                        status_code=409,
                        detail="start blocked: measured battery below safe "
                               "threshold (critical risk alert open)")
                conn.execute(
                    "UPDATE assignments SET status='running',"
                    " started_at=datetime('now') WHERE id=?",
                    (assignment_id,))
                conn.execute("UPDATE robots SET status='running' WHERE id=?",
                             (row["robot_id"],))
                conn.execute("UPDATE tasks SET status='running' WHERE id=?",
                             (row["task_id"],))
                conn.execute(
                    "INSERT INTO dispatch_logs(assignment_id,action,payload_json)"
                    " VALUES (?,?,?)",
                    (assignment_id, "start_simulated",
                     json.dumps({"simulated": True,
                                 "note": "No real control command emitted."})))
                out = _assignment(conn.execute(
                    "SELECT * FROM assignments WHERE id=?",
                    (assignment_id,)).fetchone())
                return out
        finally:
            conn.close()


def complete_assignment(assignment_id: str, token: str) -> dict:
    with WRITE_LOCK:
        conn = connect()
        try:
            with transaction(conn):
                row = _authenticate_assignment_token(conn, assignment_id, token)
                if row["status"] != "running":
                    from fastapi import HTTPException
                    raise HTTPException(
                        status_code=409,
                        detail=f"cannot complete from status {row['status']}")
                task = conn.execute("SELECT * FROM tasks WHERE id=?",
                                    (row["task_id"],)).fetchone()
                # Robot is at delivery point; charger leg is next.
                conn.execute(
                    "UPDATE robots SET status='charging',pos_x=?,pos_y=?,"
                    " at_risk=0 WHERE id=?",
                    (task["delivery_x"], task["delivery_y"], row["robot_id"]))
                conn.execute(
                    "UPDATE assignments SET status='charging',"
                    " completed_at=datetime('now') WHERE id=?",
                    (assignment_id,))
                conn.execute("UPDATE tasks SET status='done' WHERE id=?",
                             (row["task_id"],))
                conn.execute(
                    "INSERT INTO dispatch_logs(assignment_id,action,payload_json)"
                    " VALUES (?,?,?)",
                    (assignment_id, "complete_simulated", json.dumps({
                        "simulated": True,
                        "note": "Task delivered; robot queued for charging. "
                                "No real control command emitted.",
                    })))
                return _assignment(conn.execute(
                    "SELECT * FROM assignments WHERE id=?",
                    (assignment_id,)).fetchone())
        finally:
            conn.close()


def unplug_assignment(assignment_id: str, token: str) -> dict:
    """Robot finished charging: release charger slot and free the robot."""
    with WRITE_LOCK:
        conn = connect()
        try:
            with transaction(conn):
                row = _authenticate_assignment_token(conn, assignment_id, token)
                if row["status"] != "charging":
                    from fastapi import HTTPException
                    raise HTTPException(
                        status_code=409,
                        detail=f"cannot unplug from status {row['status']}")
                conn.execute(
                    "UPDATE assignments SET status='done' WHERE id=?",
                    (assignment_id,))
                conn.execute("UPDATE robots SET status='done' WHERE id=?",
                             (row["robot_id"],))
                conn.execute(
                    "INSERT INTO dispatch_logs(assignment_id,action,payload_json)"
                    " VALUES (?,?,?)",
                    (assignment_id, "unplug_simulated", json.dumps({
                        "simulated": True,
                        "note": "Charger slot released. "
                                "No real control command emitted.",
                    })))
                return _assignment(conn.execute(
                    "SELECT * FROM assignments WHERE id=?",
                    (assignment_id,)).fetchone())
        finally:
            conn.close()


def cancel_assignment(assignment_id: str) -> dict:
    """Cancel a reservation *before* start: release battery + charger holds."""
    with WRITE_LOCK:
        conn = connect()
        try:
            with transaction(conn):
                row = conn.execute("SELECT * FROM assignments WHERE id=?",
                                   (assignment_id,)).fetchone()
                if row is None:
                    from fastapi import HTTPException
                    raise HTTPException(status_code=404,
                                        detail="assignment not found")
                if row["status"] != "reserved":
                    from fastapi import HTTPException
                    raise HTTPException(
                        status_code=409,
                        detail=f"only reserved assignments can be cancelled"
                               f" (current: {row['status']})")
                conn.execute(
                    "UPDATE assignments SET status='cancelled' WHERE id=?",
                    (assignment_id,))
                conn.execute("UPDATE robots SET status='idle',at_risk=0"
                             " WHERE id=?", (row["robot_id"],))
                conn.execute("UPDATE tasks SET status='pending' WHERE id=?",
                             (row["task_id"],))
                conn.execute(
                    "INSERT INTO dispatch_logs(assignment_id,action,payload_json)"
                    " VALUES (?,?,?)",
                    (assignment_id, "cancel_simulated", json.dumps({
                        "simulated": True,
                        "released_reservation": {
                            "robot": row["robot_id"],
                            "charger": row["charger_id"],
                            "predicted_soc_kwh": row["predicted_soc_kwh"],
                        },
                        "note": "Battery and charger pre-emption released; "
                                "no real control command emitted.",
                    })))
                return _assignment(conn.execute(
                    "SELECT * FROM assignments WHERE id=?",
                    (assignment_id,)).fetchone())
        finally:
            conn.close()


# ---------------------------------------------------------------------------
# measured battery vs prediction
# ---------------------------------------------------------------------------

def report_telemetry(assignment_id: str, token: str,
                     measured_soc_kwh: float, note: str | None) -> dict:
    with WRITE_LOCK:
        conn = connect()
        try:
            with transaction(conn):
                row = _authenticate_assignment_token(conn, assignment_id, token)
                predicted = row["predicted_soc_kwh"]
                ratio = (measured_soc_kwh / predicted
                         if predicted > 0 else float("inf"))
                if ratio < config.MEASURED_CRITICAL_RATIO:
                    severity = "critical"
                elif ratio < config.MEASURED_WARN_RATIO:
                    severity = "warning"
                else:
                    severity = "ok"

                cur = conn.execute(
                    "INSERT INTO telemetry(assignment_id,measured_soc_kwh,"
                    "predicted_soc_kwh,deviation_ratio,severity,note)"
                    " VALUES (?,?,?,?,?,?)",
                    (assignment_id, measured_soc_kwh, predicted,
                     round(ratio, 6), severity, note))
                telemetry_id = cur.lastrowid

                result = {
                    "telemetry_id": telemetry_id,
                    "assignment_id": assignment_id,
                    "measured_soc_kwh": measured_soc_kwh,
                    "predicted_soc_kwh": predicted,
                    "deviation_ratio": round(ratio, 6),
                    "severity": severity,
                    "required_soc_kwh": row["required_soc_kwh"],
                }

                if severity == "critical":
                    conn.execute("UPDATE robots SET at_risk=1 WHERE id=?",
                                 (row["robot_id"],))
                    conn.execute(
                        "INSERT INTO alerts(kind,severity,entity_type,entity_id,"
                        "message,detail_json) VALUES (?,?,?,?,?,?)",
                        ("battery_risk", "critical", "assignment",
                         assignment_id,
                         f"Measured SoC {measured_soc_kwh:.3f} kWh is below "
                         f"90% of predicted {predicted:.3f} kWh; task start "
                         f"blocked and replanning advised.",
                         json.dumps(result)))
                    result["blocked_start"] = True
                elif severity == "warning":
                    conn.execute(
                        "INSERT INTO alerts(kind,severity,entity_type,entity_id,"
                        "message,detail_json) VALUES (?,?,?,?,?,?)",
                        ("battery_risk", "warning", "assignment",
                         assignment_id,
                         f"Measured SoC {measured_soc_kwh:.3f} kWh is below "
                         f"predicted {predicted:.3f} kWh (ratio "
                         f"{ratio:.1%}); monitor closely.",
                         json.dumps(result)))
                return result
        finally:
            conn.close()


# ---------------------------------------------------------------------------
# charger failure + recomputation
# ---------------------------------------------------------------------------

def _reachable_alternatives(conn, robot_row, from_pos, soc_kwh) -> list[dict]:
    """Real reachability check from an arbitrary position (for running tasks)."""
    from .energy import distance, leg_energy, robot_safety_margin
    out = []
    margin = robot_safety_margin(robot_row["capacity_kwh"])
    for c in conn.execute(
            "SELECT * FROM chargers WHERE status='available'"):
        d = distance(from_pos, (c["pos_x"], c["pos_y"]))
        need = leg_energy(d, 0.0, loaded=False) + margin
        out.append({
            "charger_id": c["id"], "distance_m": round(d, 4),
            "needed_soc_kwh": round(need, 6),
            "reachable": soc_kwh >= need,
        })
    out.sort(key=lambda x: x["distance_m"])
    return out


def charger_failover(charger_id: str) -> dict:
    """Mark charger failed; free unstarted work, recompute, alert on running."""
    with WRITE_LOCK:
        conn = connect()
        try:
            with transaction(conn):
                charger = conn.execute("SELECT * FROM chargers WHERE id=?",
                                       (charger_id,)).fetchone()
                if charger is None:
                    from fastapi import HTTPException
                    raise HTTPException(status_code=404,
                                        detail="charger not found")
                if charger["status"] == "failed":
                    return {"charger_id": charger_id, "already_failed": True,
                            "reassignments": [], "risk_alerts": []}
                conn.execute(
                    "UPDATE chargers SET status='failed' WHERE id=?",
                    (charger_id,))

                # 1) unstarted assignments -> release everything atomically
                affected_rows = conn.execute(
                    "SELECT * FROM assignments WHERE charger_id=? AND status='reserved'",
                    (charger_id,)).fetchall()
                affected_task_ids: set[str] = set()
                for a in affected_rows:
                    conn.execute(
                        "UPDATE assignments SET status='cancelled' WHERE id=?",
                        (a["id"],))
                    conn.execute(
                        "UPDATE robots SET status='idle' WHERE id=?",
                        (a["robot_id"],))
                    conn.execute(
                        "UPDATE tasks SET status='pending' WHERE id=?",
                        (a["task_id"],))
                    conn.execute(
                        "INSERT INTO dispatch_logs(assignment_id,action,"
                        "payload_json) VALUES (?,?,?)",
                        (a["id"], "release_on_charger_failure", json.dumps({
                            "simulated": True, "charger": charger_id,
                            "reason": "charger failed before task start; "
                                      "reservation rolled back.",
                        })))
                    affected_task_ids.add(a["task_id"])

                # 2) running / charging assignments are KEPT -> risk alerts
                running = conn.execute(
                    "SELECT * FROM assignments WHERE charger_id=? AND status "
                    "IN ('running','charging')", (charger_id,)).fetchall()
                risk_alerts = []
                for a in running:
                    robot = conn.execute("SELECT * FROM robots WHERE id=?",
                                         (a["robot_id"],)).fetchone()
                    pos = (robot["pos_x"], robot["pos_y"])
                    alts = _reachable_alternatives(
                        conn, robot, pos, robot["current_soc_kwh"])
                    reachable = [x for x in alts if x["reachable"]]
                    detail = {
                        "assignment_id": a["id"], "robot": a["robot_id"],
                        "task": a["task_id"], "failed_charger": charger_id,
                        "alternatives": alts,
                        "safe_alternatives": reachable,
                        "reservation_kept": True,
                    }
                    conn.execute(
                        "INSERT INTO alerts(kind,severity,entity_type,entity_id,"
                        "message,detail_json) VALUES (?,?,?,?,?,?)",
                        ("charger_failure", "critical", "assignment", a["id"],
                         f"Charger {charger_id} failed while robot "
                         f"{a['robot_id']} is {a['status']}; task retained. "
                         f"{len(reachable)} reachable alternative charger(s).",
                         json.dumps(detail)))
                    risk_alerts.append(detail)

                released = [
                    {"assignment_id": a["id"], "robot": a["robot_id"],
                     "task": a["task_id"]} for a in affected_rows
                ]

            # 3) recompute all tasks freed by the failure (new transaction)
            reassign_result = _allocate(conn, only_task_ids=affected_task_ids)

            return {
                "charger_id": charger_id,
                "released_reservations": released,
                "reassignments": reassign_result["assignments"],
                "unassigned_after_recompute": reassign_result["unassigned"],
                "risk_alerts": risk_alerts,
                "simulated": True,
            }
        finally:
            conn.close()
