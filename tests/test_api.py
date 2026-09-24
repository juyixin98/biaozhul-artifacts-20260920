"""HTTP/服务层验收测试：窄道边冲突、终点占用、撤销、版本乐观锁、批处理原子性。"""

from __future__ import annotations

from app import db as dbmod
from app.main import DB_PATH


def reset_corridor(client, width=4, height=1, r1=(0, 0), r2=(3, 0)):
    """配置 width x height 无障碍窄道地图，并把 R1/R2 放到指定起点。"""
    r = client.put(
        "/map", json={"width": width, "height": height, "obstacles": []}
    )
    assert r.status_code == 200, r.text
    client.put("/robots/R1", json={"start": {"x": r1[0], "y": r1[1]}})
    client.put("/robots/R2", json={"start": {"x": r2[0], "y": r2[1]}})
    return r.json()["map_version"]


def simulate(reservations):
    """对已提交预约做独立的逐刻模拟，返回 (顶点冲突, 边冲突) 列表。"""
    paths = {r["robot_id"]: [tuple(c) for c in r["path"]] for r in reservations}
    max_len = max((len(p) for p in paths.values()), default=0)
    vertex, edge = [], []
    for t in range(max_len):
        cells = {}
        moves = {}
        for rid, p in paths.items():
            cell = p[t] if t < len(p) else p[-1]
            cells.setdefault(cell, []).append(rid)
            if t < len(p) - 1:
                moves[rid] = (p[t], p[t + 1])
        for cell, rids in cells.items():
            if len(rids) > 1:
                vertex.append((t, cell, rids))
        rids = list(moves)
        for i in range(len(rids)):
            for j in range(i + 1, len(rids)):
                a, b = rids[i], rids[j]
                if moves[a][0] == moves[b][1] and moves[a][1] == moves[b][0]:
                    if moves[a][0] != moves[a][1]:  # 等待不算交换
                        edge.append((t, a, b, moves[a]))
    return vertex, edge


def test_health_state_reset(client):
    assert client.get("/health").json()["status"] == "ok"
    s = client.get("/state").json()
    assert s["map"]["version"] == 1
    assert s["reservation_version"] == 0
    assert len(s["robots"]) == 8
    assert sorted(r["robot_id"] for r in s["robots"]) == [f"R{i}" for i in range(1, 9)]


def test_single_plan_happy_path_and_actions(client):
    reset_corridor(client, width=6, height=1)
    r = client.post(
        "/reservations/plan",
        json={"robot_id": "R1", "goal": {"x": 5, "y": 0}},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["arrival_time"] == 5
    assert body["path"][0] == [0, 0] and body["path"][-1] == [5, 0]
    assert all(a["type"] == "move" for a in body["actions"])
    assert body["map_version"] >= 1
    assert "cancel_token" in body and "." in body["cancel_token"]
    # 重复规划同一忙碌机器人 -> 409
    r2 = client.post(
        "/reservations/plan", json={"robot_id": "R1", "goal": {"x": 1, "y": 0}}
    )
    assert r2.status_code == 409 and r2.json()["error"]["code"] == "ROBOT_BUSY"


def test_bidirectional_narrow_corridor_edge_conflict(client):
    """双向窄道：R2 先从右端预约到左端（终点永久占用左端），随后 R1 想从左端到右端。
    顶点表任何刻都不重合，但对向交换封死走廊 -> 必须 409 且证据含 edge。"""
    reset_corridor(client, width=4, height=1)

    ok = client.post(
        "/reservations/plan", json={"robot_id": "R2", "goal": {"x": 0, "y": 0}}
    )
    assert ok.status_code == 200, ok.text

    blocked = client.post(
        "/reservations/plan", json={"robot_id": "R1", "goal": {"x": 3, "y": 0}}
    )
    assert blocked.status_code == 409, blocked.text
    err = blocked.json()["error"]
    assert err["code"] == "NO_FEASIBLE_PATH"
    blockers = err["details"]["evidence"]["blockers"]
    edge_evidence = [b for b in blockers if b["type"] == "edge"]
    assert edge_evidence, f"证据中缺少边交换冲突：{blockers}"
    # 已有预约本身依然合法
    v, e = simulate(client.get("/state").json()["reservations"])
    assert not v and not e


def test_goal_occupied_permanent(client):
    reset_corridor(client, width=5, height=1)
    client.post("/reservations/plan", json={"robot_id": "R1", "goal": {"x": 4, "y": 0}})
    r = client.post(
        "/reservations/plan", json={"robot_id": "R2", "goal": {"x": 4, "y": 0}}
    )
    assert r.status_code == 409
    err = r.json()["error"]
    assert err["code"] == "NO_FEASIBLE_PATH"
    assert err["details"]["evidence"]["kind"] == "permanent_goal"


def test_cancel_then_reschedule_and_token_security(client):
    reset_corridor(client, width=5, height=1)
    made = client.post(
        "/reservations/plan", json={"robot_id": "R1", "goal": {"x": 4, "y": 0}}
    ).json()
    rid, token = made["resv_id"], made["cancel_token"]
    ver_after_plan = made["reservation_version"]

    # 篡改 token -> 403
    tampered = token[:-2] + ("aa" if not token.endswith("aa") else "bb")
    r = client.post(f"/reservations/{rid}/cancel", json={"cancel_token": tampered})
    assert r.status_code == 403 and r.json()["error"]["code"] == "INVALID_TOKEN"

    # 用别的预约 id 路径 + 本 token -> 403 mismatch
    r = client.post(
        "/reservations/" + ("0" * 32) + "/cancel", json={"cancel_token": token}
    )
    assert r.status_code == 403 and r.json()["error"]["code"] == "TOKEN_RESERVATION_MISMATCH"

    # 正确撤销
    r = client.post(f"/reservations/{rid}/cancel", json={"cancel_token": token})
    assert r.status_code == 200, r.text
    assert r.json()["reservation_version"] == ver_after_plan + 1

    # 重复撤销 -> 409
    r = client.post(f"/reservations/{rid}/cancel", json={"cancel_token": token})
    assert r.status_code == 409 and r.json()["error"]["code"] == "RESV_NOT_ACTIVE"

    # 腾出终点后 R2 能进入该格
    r = client.post(
        "/reservations/plan", json={"robot_id": "R2", "goal": {"x": 4, "y": 0}}
    )
    assert r.status_code == 200, r.text


def test_optimistic_versions_stale_rejected(client):
    reset_corridor(client, width=5, height=1)
    # 预期预约版本超前
    r = client.post(
        "/reservations/plan",
        json={
            "robot_id": "R1",
            "goal": {"x": 1, "y": 0},
            "expected_reservation_version": 99,
        },
    )
    assert r.status_code == 412 and r.json()["error"]["code"] == "RESV_STALE"

    # 预期地图版本落后
    r = client.post(
        "/reservations/plan",
        json={
            "robot_id": "R1",
            "goal": {"x": 1, "y": 0},
            "expected_map_version": 1,
        },
    )
    assert r.status_code == 412 and r.json()["error"]["code"] == "MAP_STALE"

    # 版本正确则成功
    mv = client.get("/state").json()["map"]["version"]
    r = client.post(
        "/reservations/plan",
        json={
            "robot_id": "R1",
            "goal": {"x": 1, "y": 0},
            "expected_map_version": mv,
            "expected_reservation_version": 0,
        },
    )
    assert r.status_code == 200, r.text


def test_map_change_during_planning_is_revalidated(client):
    """规划期间（快照之后、搜索之前）地图被他人修改 -> 提交复核 412，绝不落库。"""
    reset_corridor(client, width=6, height=1)

    from app import main

    def hook(snapshot):
        other = dbmod.connect(DB_PATH)
        try:
            from app.planner import GridMap

            with dbmod.immediate_tx(other):
                # 在 R1 必经的 (3,0) 放障碍
                dbmod.replace_map(other, GridMap(6, 1, [(3, 0)]))
        finally:
            other.close()

    main.app.state.before_search_hook = hook
    r = client.post(
        "/reservations/plan", json={"robot_id": "R1", "goal": {"x": 5, "y": 0}}
    )
    assert r.status_code == 412, r.text
    assert r.json()["error"]["code"] == "MAP_CHANGED_DURING_PLANNING"
    state = client.get("/state").json()
    assert state["reservations"] == []


def test_batch_atomicity_and_priority_evidence(client):
    reset_corridor(client, width=4, height=1)
    # R1 左->右、R2 右->左：固定优先级下 R2 失败，整个批次必须一个都不写入
    r = client.post(
        "/reservations/plan-batch",
        json={
            "goals": {
                "R1": {"goal": {"x": 3, "y": 0}},
                "R2": {"goal": {"x": 0, "y": 0}},
            }
        },
    )
    assert r.status_code == 409, r.text
    err = r.json()["error"]
    assert err["code"] == "NO_FEASIBLE_PATH"
    assert err["details"]["failed_robot"] == "R2"
    assert err["details"]["priority_order"] == ["R1", "R2"]
    assert any(
        b["type"] == "edge" for b in err["details"]["failure"]["evidence"]["blockers"]
    )
    state = client.get("/state").json()
    assert state["reservations"] == []
    assert state["reservation_version"] == 0


def test_batch_success_on_bay_map_independently_simulated(client):
    client.post("/admin/reset")  # 默认带会让湾的地图
    r = client.post(
        "/reservations/plan-batch",
        json={
            "goals": {
                "R1": {"goal": {"x": 7, "y": 0}},
                "R8": {"goal": {"x": 0, "y": 4}},
            }
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["priority_order"] == ["R1", "R8"]
    assert body["reservation_version"] == 2
    resvs = client.get("/state").json()["reservations"]
    v, e = simulate(resvs)
    assert not v and not e


def test_cancel_frees_goal_for_followup_in_corridor(client):
    """撤销后窄道恢复通行：撤销 R2 的横穿预约后，R1 可以走完全程。"""
    reset_corridor(client, width=4, height=1)
    made = client.post(
        "/reservations/plan", json={"robot_id": "R2", "goal": {"x": 0, "y": 0}}
    ).json()
    blocked = client.post(
        "/reservations/plan", json={"robot_id": "R1", "goal": {"x": 3, "y": 0}}
    )
    assert blocked.status_code == 409
    client.post(
        f"/reservations/{made['resv_id']}/cancel",
        json={"cancel_token": made["cancel_token"]},
    )
    ok = client.post(
        "/reservations/plan", json={"robot_id": "R1", "goal": {"x": 3, "y": 0}}
    )
    assert ok.status_code == 200, ok.text


def test_cannot_move_robot_with_active_reservation(client):
    reset_corridor(client, width=5, height=1)
    client.post("/reservations/plan", json={"robot_id": "R1", "goal": {"x": 4, "y": 0}})
    r = client.put("/robots/R1", json={"start": {"x": 2, "y": 0}})
    assert r.status_code == 409 and r.json()["error"]["code"] == "ROBOT_BUSY"


def test_max_robots_enforced(client):
    reset_corridor(client, width=20, height=1)
    r = client.put("/robots/R9", json={"start": {"x": 9, "y": 0}})
    assert r.status_code == 409 and r.json()["error"]["code"] == "TOO_MANY_ROBOTS"


def test_map_update_reports_affected_reservations(client):
    reset_corridor(client, width=6, height=1)
    client.post("/reservations/plan", json={"robot_id": "R1", "goal": {"x": 5, "y": 0}})
    r = client.put(
        "/map",
        json={"width": 6, "height": 1, "obstacles": [{"x": 5, "y": 0}]},
    )
    assert r.status_code == 200
    affected = r.json()["affected_reservations"]
    assert len(affected) == 1 and affected[0]["robot_id"] == "R1"
