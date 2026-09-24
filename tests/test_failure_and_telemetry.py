"""充电点失效（重算未开始 / 运行中保留告警）与实测电量低于预测。"""
from __future__ import annotations


def _setup_two_allocated(api, *, start_t1=True):
    # T1 预占 C1 并开始执行（R1）；T2 未开始，预占 C2（R2）
    api.robot("R1", 0, 0, 200.0)
    api.robot("R2", 2, 0, 200.0)
    api.charger("C1", 0, 0)
    api.charger("C2", 30, 0)
    api.task("T1", 50, 0, 0.0, 60.0)   # 25.2Wh
    api.task("T2", 30, 0, 0.0, 60.0)
    d = api.dispatch(["T1", "T2"]).json()
    assert len(d["assigned"]) == 2
    assert {(a["task_id"], a["charger_id"])
            for a in d["assigned"]} == {("T1", "C1"), ("T2", "C2")}
    if start_t1:
        api.post("/api/tasks/T1/start")
    return d


def test_failed_charger_on_running_mission_keeps_and_warns(api):
    _setup_two_allocated(api)
    r = api.post("/api/chargers/C1/fail",
                 {"reason": "grid outage"}).json()

    # 运行中的 T1：保留（不取消、不重算），并给出 critical 风险告警
    assert r["released_for_recompute"] == []
    assert r["recompute"] is None
    kept = {x["task_id"]: x for x in r["running_missions_kept_at_risk"]}
    assert set(kept) == {"T1"}
    assert kept["T1"]["robot_id"] == "R1"

    snap = api.get("/api/snapshot").json()
    active = {(a["task_id"], a["status"], a["charger_id"])
              for a in snap["active_allocations"]}
    assert ("T1", "running", "C1") in active
    assert ("T2", "planned", "C2") in active

    alerts = api.get("/api/alerts").json()
    risk = [a for a in alerts
            if a["kind"] == "running_mission_charger_failed_risk"]
    assert len(risk) == 1 and risk[0]["severity"] == "critical"


def test_failed_charger_releases_planned_and_recomputes(api):
    _setup_two_allocated(api)
    # 仅 C2 还不够，再造一个 C3 作为重算落点
    api.charger("C3", 40, 0)
    r = api.post("/api/chargers/C2/fail",
                 {"reason": "hardware fault"}).json()

    # 未开始的 T2 释放并自动重算到 C3；运行中的 T1 不受影响
    assert r["released_for_recompute"] == ["T2"]
    assert r["running_missions_kept_at_risk"] == []
    recompute = r["recompute"]
    assert {a["task_id"] for a in recompute["assigned"]} == {"T2"}
    assert recompute["assigned"][0]["charger_id"] == "C3"

    snap = api.get("/api/snapshot").json()
    active = {(a["task_id"], a["status"], a["charger_id"])
              for a in snap["active_allocations"]}
    assert ("T1", "running", "C1") in active
    assert ("T2", "planned", "C3") in active

    alerts = api.get("/api/alerts").json()
    assert any(a["kind"] == "reservation_released_recompute"
               for a in alerts)


def test_charger_failure_without_alternative_leaves_pending(api):
    """失效后没有别的可用充电点 -> 释放的任务保持 pending 并如实报告。"""
    api.robot("R1", 0, 0, 101.0)
    api.charger("C1", 0, 0)
    api.task("T1", 50, 0, 0.0, 60.0)
    api.dispatch(["T1"])
    r = api.post("/api/chargers/C1/fail", {"reason": "x"}).json()
    assert r["released_for_recompute"] == ["T1"]
    assert r["recompute"]["assigned"] == []
    assert r["recompute"]["unassigned"] == [
        {"task_id": "T1", "reason": "no_available_charger"}
    ]
    snap = api.get("/api/snapshot").json()
    assert snap["tasks"][0]["status"] == "pending"


def test_fail_unknown_or_double_fail(api):
    api.charger("C1", 0, 0)
    assert api.post("/api/chargers/NOPE/fail",
                    {"reason": "x"}).status_code == 404
    api.post("/api/chargers/C1/fail", {"reason": "x"})
    assert api.post("/api/chargers/C1/fail",
                    {"reason": "x"}).status_code == 409


# ---------------------------------------------------------------- 遥测

def test_measured_battery_below_predicted_raises_alert(api):
    api.robot("R1", 0, 0, 101.0)
    api.charger("C1", 0, 0)
    api.task("T1", 50, 0, 20.0, 0.0)  # 去程 0.192*50=9.6, 返航 9
    api.dispatch(["T1"])
    start = api.post("/api/tasks/T1/start").json()
    assert start["start_battery_wh"] == 101.0

    # 走完去程 50m：预测剩余 101 - 9.6 = 91.4；上报 85 -> 低于预测
    tel = api.post("/api/robots/R1/telemetry", {
        "battery_wh": 85.0,
        "x": 50, "y": 0,
        "mission_completed_distance_m": 50.0,
    }).json()
    assert tel["predicted_remaining_wh"] == 91.4
    assert len(tel["alerts"]) == 1

    alerts = api.get("/api/alerts", ).json()
    low = next(a for a in alerts
               if a["kind"] == "battery_lower_than_predicted")
    assert low["severity"] == "critical"
    assert low["robot_id"] == "R1"
    assert low["task_id"] == "T1"


def test_measured_battery_in_line_with_prediction_no_alert(api):
    api.robot("R1", 0, 0, 101.0)
    api.charger("C1", 0, 0)
    api.task("T1", 50, 0, 20.0, 0.0)
    api.dispatch(["T1"])
    api.post("/api/tasks/T1/start")

    tel = api.post("/api/robots/R1/telemetry", {
        "battery_wh": 91.2,  # 与预测 91.4 的差在 1% 容差内
        "x": 50, "y": 0,
        "mission_completed_distance_m": 50.0,
    }).json()
    assert tel["alerts"] == []
    assert api.get("/api/alerts").json() == []


def test_complete_after_telemetry_settles_position_and_state(api):
    api.robot("R1", 0, 0, 101.0)
    api.charger("C1", 30, 0)
    api.task("T1", 50, 0, 0.0, 0.0)
    api.dispatch(["T1"])
    api.post("/api/tasks/T1/start")
    done = api.post("/api/tasks/T1/complete",
                    {"measured_battery_wh": 55.0}).json()
    assert done["status"] == "completed"
    assert done["battery_wh_source"] == "measured"
    snap = api.get("/api/snapshot").json()
    r1 = next(r for r in snap["robots"] if r["id"] == "R1")
    assert r1["status"] == "idle"
    assert (r1["x"], r1["y"]) == (30.0, 0.0)
    assert r1["battery_wh"] == 55.0
