"""核心业务场景（题目点名）：

- 局部最便宜但无法返航
- 充电点竞争（含唯一约束）
- 取消预占并重新分配
- 重复接单防护
- 分配/预占同事务
- 多机器人最小成本可解释匹配
"""
from __future__ import annotations

import pytest

from app import crypto, database


def edge_for(resp, task_id, robot_id):
    es = [e for e in resp["candidate_edges"]
          if e["task_id"] == task_id and e["robot_id"] == robot_id]
    return es[-1]


def assignment_for(resp, task_id):
    return next(a for a in resp["assigned"] if a["task_id"] == task_id)


# --------------------------------------------------------------- 场景 1

def test_locally_cheapest_robot_cannot_return_is_rejected(api):
    """RN 在任务点旁边（去程局部最便宜），但满电也不够返航+余量；
    RF 在充电点旁（返航 0），电量充足 -> 必须选 RF。"""
    api.robot("RN", 50, 0, 15.0)    # 去程≈0，但返航 50m 空载需 9Wh... 见下
    api.robot("RF", 0, 0, 300.0)
    api.charger("C0", 0, 0)
    api.task("T1", 50, 0, 10.0, 0.0)

    # RN：去程满载 ~0（在任务点），返航 50m*0.18=9Wh，余量 20 -> 需 29Wh，15 不够
    resp = api.dispatch(["T1"]).json()
    e_n = edge_for(resp, "T1", "RN")
    e_f = edge_for(resp, "T1", "RF")
    assert e_n["feasible"] is False
    assert e_n["reason"] == "insufficient_battery_after_safety_margin"
    assert e_f["feasible"] is True
    assert assignment_for(resp, "T1")["robot_id"] == "RF"
    assert resp["unassigned"] == []


def test_mission_that_just_finishes_without_margin_rejected(api):
    """电量只够勉强完成任务、无安全余量 -> 不可分配。"""
    # 总能耗 10Wh，余量 20：给 29Wh（差 1Wh）
    api.robot("R1", 0, 0, 29.0)
    api.charger("C1", 0, 0)
    # 任务 50m 外空载往返 = 0.18*100 = 18Wh → 29 不够
    api.task("T1", 50, 0, 0.0, 0.0)
    resp = api.dispatch(["T1"]).json()
    assert resp["assigned"] == []
    assert resp["unassigned"][0]["reason"] == "no_feasible_robot"


# --------------------------------------------------------------- 场景 2：竞争

def test_charger_competition_two_tasks_one_charger(api):
    """两个任务都只能靠同一个充电点返航：先占先得，第二个明确报竞争。"""
    api.robot("R1", 0, 0, 400.0)
    api.robot("R2", 1, 0, 400.0)
    api.charger("C1", 0, 0)
    api.task("T1", 60, 0, 0.0, 0.0)
    api.task("T2", 50, 0, 0.0, 0.0)

    resp = api.dispatch(["T1", "T2"]).json()
    assert len(resp["assigned"]) == 1
    # 较便宜的 T2（往返更短）先得
    assert assignment_for(resp, "T2")["charger_id"] == "C1"
    u = {x["task_id"]: x["reason"] for x in resp["unassigned"]}
    assert u == {"T1": "no_available_charger"}


def test_charger_competition_recompute_to_other_charger(api):
    """首选充电点在同批次被更便宜的边占用后，剩余边就剩余充电点重算。"""
    # C1 在原点近，C2 在 (30,0)
    api.charger("C1", 0, 0)
    api.charger("C2", 30, 0)
    api.robot("R1", 0, 0, 100000.0)
    api.robot("R2", 0, 0, 100000.0)
    # T2 在 10m（更便宜先选，首选 C1）；T1 在 15m，到 C1/C2 等距，
    # 按充电点 id 次序首选 C1 —— 被占后必须重算到 C2。
    api.task("T2", 10, 0, 0.0, 0.0)
    api.task("T1", 15, 0, 0.0, 0.0)

    resp = api.dispatch(["T1", "T2"]).json()
    assert {a["task_id"]: a["charger_id"] for a in resp["assigned"]} == {
        "T1": "C2", "T2": "C1",
    }
    t1r2 = edge_for(resp, "T1", "R2")
    assert t1r2["charger_id"] == "C2"
    assert "claimed earlier" in (t1r2["note"] or "")


def test_db_unique_index_blocks_charger_double_claim(api):
    """数据库部分唯一索引兜底：同一充电点不能被两条活跃分配预占。"""
    api.robot("R1", 0, 0, 100000.0)
    api.robot("R2", 0, 0, 100000.0)
    api.charger("C1", 0, 0)
    api.task("T1", 10, 0, 0.0, 0.0)
    api.task("T2", 20, 0, 0.0, 0.0)
    r = api.dispatch(["T1"]).json()
    assert len(r["assigned"]) == 1

    # 绕过服务层直接尝试插入竞争预占 -> 必须被唯一索引拒绝
    conn = database.connect()
    try:
        with pytest.raises(Exception):
            with database.immediate_tx(conn):
                conn.execute(
                    "INSERT INTO allocations(task_id,robot_id,charger_id,"
                    "distance_out_m,distance_return_m,outbound_energy_wh,"
                    "wait_energy_wh,return_energy_wh,total_energy_wh,"
                    "safety_margin_wh,reserved_energy_wh,status,created_at)"
                    " VALUES ('T2','R2','C1',1,1,1,1,1,1,4,5,'planned',?)",
                    (database.utcnow(),),
                )
    finally:
        conn.close()


# --------------------------------------------------------------- 场景 3：取消

def test_cancel_releases_reservation_and_allows_recompute(api):
    api.robot("R1", 0, 0, 400.0)
    api.charger("C1", 0, 0)
    api.task("T1", 40, 0, 0.0, 0.0)
    d = api.dispatch(["T1"]).json()
    assert len(d["assigned"]) == 1

    cancel = api.post("/api/tasks/T1/cancel", {"reason": "test"}).json()
    assert cancel["status"] == "pending"
    assert cancel["released_charger_id"] == "C1"

    # 机器人恢复 idle、充电点解除预占 -> 同一任务可再次分配
    snap = api.get("/api/snapshot").json()
    assert snap["active_allocations"] == []
    assert snap["robots"][0]["status"] == "idle"

    d2 = api.dispatch(["T1"]).json()
    assert len(d2["assigned"]) == 1
    assert assignment_for(d2, "T1")["task_id"] == "T1"


def test_cancel_running_task_is_rejected(api):
    api.robot("R1", 0, 0, 400.0)
    api.charger("C1", 0, 0)
    api.task("T1", 40, 0, 0.0, 0.0)
    api.dispatch(["T1"])
    api.post("/api/tasks/T1/start")
    r = api.post("/api/tasks/T1/cancel")
    assert r.status_code == 409


# --------------------------------------------------------------- 防重复接单

def test_task_cannot_be_assigned_twice(api):
    api.robot("R1", 0, 0, 400.0)
    api.robot("R2", 1, 0, 400.0)
    api.charger("C1", 0, 0)
    api.charger("C2", 30, 0)
    api.task("T1", 40, 0, 0.0, 0.0)
    assert api.dispatch(["T1"]).status_code == 200
    r = api.dispatch(["T1"])
    assert r.status_code == 409
    assert "not pending" in r.json()["detail"]


def test_one_robot_cannot_take_two_tasks(api):
    api.robot("R1", 0, 0, 100000.0)
    api.charger("C1", 0, 0)
    api.charger("C2", 30, 0)
    api.task("T1", 10, 0, 0.0, 0.0)
    api.task("T2", 20, 0, 0.0, 0.0)
    resp = api.dispatch(["T1", "T2"]).json()
    assert len(resp["assigned"]) == 1
    robots_used = {a["robot_id"] for a in resp["assigned"]}
    assert robots_used == {"R1"}


# --------------------------------------------------------------- 匹配可解释性

def test_matching_picks_minimum_cost_and_explains_all_edges(api):
    # R1 在原点，R2 在 (8,0) 离任务更近；两者都可达 -> 必须选更便宜的 R2
    api.robot("R1", 0, 0, 100000.0)
    api.robot("R2", 8, 0, 100000.0)
    api.charger("C1", 0, 0)
    api.task("T1", 40, 0, 10.0, 30.0)

    resp = api.dispatch(["T1"]).json()
    chosen = assignment_for(resp, "T1")
    assert chosen["robot_id"] == "R2"
    # 全部候选边都带完整能耗拆解
    for e in resp["candidate_edges"]:
        if e["energy"]:
            for k in ("distance_out_m", "distance_return_m",
                      "outbound_energy_wh", "wait_energy_wh",
                      "return_energy_wh", "total_energy_wh"):
                assert k in e["energy"]
    # 结算字段被 HMAC 回执覆盖，且签名可验真
    rcpt = chosen["receipt"]
    assert rcpt["algorithm"] == "HMAC-SHA256"
    assert crypto.verify_receipt(rcpt["fields"], rcpt["signature"])


def test_reservation_covers_total_plus_margin(api):
    api.robot("R1", 0, 0, 100000.0)
    api.charger("C1", 0, 0)
    api.task("T1", 40, 0, 10.0, 30.0)
    resp = api.dispatch(["T1"], margin=25.0).json()
    a = assignment_for(resp, "T1")
    assert a["reserved_energy_wh"] == pytest.approx(
        a["energy"]["total_energy_wh"] + 25.0
    )
    assert a["safety_margin_wh"] == 25.0


def test_simulated_command_is_never_real(api):
    api.robot("R1", 0, 0, 400.0)
    api.charger("C1", 0, 0)
    api.task("T1", 40, 0, 5.0, 0.0)
    resp = api.dispatch(["T1"]).json()
    cmd = assignment_for(resp, "T1")["simulated_command"]
    assert cmd["simulated"] is True
    assert cmd["do_not_send_to_real_robot"] is True
    assert [s["action"] for s in cmd["steps"]] == ["goto", "wait", "goto"]
