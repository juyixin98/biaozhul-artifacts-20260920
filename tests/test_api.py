"""API 端到端测试：验收四类场景（双向窄道、终点占用、撤销、并发），
以及版本乐观控制、规划期间地图变化、密码学凭证等。
"""

from __future__ import annotations

import os
import tempfile
import threading

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def client(tmp_path, monkeypatch):
    db_path = str(tmp_path / "test.db")
    monkeypatch.setenv("STP_DB_PATH", db_path)
    # 确保使用全新的应用级单例
    import app.main as main
    from app import service as service_mod

    main._db = None
    main._service = None
    service_mod.pre_search_hook = None
    with TestClient(main.app) as c:
        yield c
    service_mod.pre_search_hook = None


def _create_map(client, map_id="m", width=5, height=1, obstacles=None):
    r = client.post(
        "/api/maps",
        json={"map_id": map_id, "width": width, "height": height,
              "obstacles": obstacles or []},
    )
    assert r.status_code == 201, r.text
    return r.json()


def _plan(client, map_id, robot_id, start, goal, **kw):
    body = {"robot_id": robot_id, "start": start, "goal": goal, **kw}
    return client.post(f"/api/maps/{map_id}/reservations", json=body)


# --------------------------------------------------------------- 基础 happy path
def test_health_and_create_map(client):
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"
    m = _create_map(client, "world", 6, 3, [[2, 2]])
    assert m["map_version"] == 1
    assert m["reservation_version"] == 0
    assert len(m["content_hash"]) == 64  # sha256 hex


def test_plan_success_returns_path_actions_token_and_versions(client):
    _create_map(client, "m", 5, 1)
    r = _plan(client, "m", 1, [0, 0], [4, 0])
    assert r.status_code == 200, r.text
    j = r.json()
    assert j["planned"] is True
    assert j["path"][0] == [0, 0]
    assert j["path"][-1] == [4, 0]
    assert j["actions"][0]["type"] == "move"
    assert len(j["cancel_token"]) == 64  # 32 字节 -> 64 hex
    assert j["reservation_version"] == 1
    assert j["map_version"] == 1
    # 策略声明：固定优先级、不保证全局完备
    assert j["policy"]["globally_complete"] is False
    assert "priority" in j["policy"]


def test_map_validation_errors(client):
    _create_map(client, "m", 3, 1)
    r = _plan(client, "m", 9, [0, 0], [1, 0])
    assert r.status_code == 422  # pydantic 范围校验
    r = _plan(client, "m", 1, [5, 0], [1, 0])
    assert r.status_code == 400
    r = _plan(client, "nonexistent", 1, [0, 0], [1, 0])
    assert r.status_code == 404
    # 障碍挡住 goal
    _create_map(client, "obs", 3, 1, [[1, 0]])
    r = _plan(client, "obs", 1, [0, 0], [1, 0])
    assert r.status_code == 409
    assert r.json()["error"]["details"]["evidence"]["type"] in (
        "GOAL_ON_OBSTACLE",)


# --------------------------------------------------------------- 验收 1：双向窄道
def test_bidirectional_narrow_corridor_returns_conflict_evidence(client):
    _create_map(client, "corr", 5, 1)
    r1 = _plan(client, "corr", 1, [0, 0], [4, 0])
    assert r1.status_code == 200
    # r2 反方向：窄道中无处避让，且 r1 终点永久占 (4,0)
    r2 = _plan(client, "corr", 2, [4, 0], [0, 0])
    assert r2.status_code == 409, r2.text
    body = r2.json()["error"]["details"]
    ev = body["evidence"]
    # 证据必须指向真实冲突，并包含固定优先级说明
    assert ev["with_robot"] == 1
    assert ev["type"] in ("NO_PATH_IN_HORIZON", "ENDPOINT_OCCUPIED")
    assert body["policy"]["globally_complete"] is False
    # 根因必须是顶点或边冲突之一，不能是空泛失败
    if ev["type"] == "NO_PATH_IN_HORIZON":
        assert ev["root_cause"] in (
            "VERTEX_CONFLICT", "EDGE_CONFLICT_SWAP", "ENDPOINT_OCCUPIED")


def test_edge_swap_is_detected_in_minimal_corridor(client):
    """2 格最小对向交换：顶点检查会漏掉，必须 409 且根因是 EDGE_CONFLICT_SWAP。"""
    _create_map(client, "swap", 2, 1)
    r1 = _plan(client, "swap", 1, [0, 0], [1, 0], horizon=1)
    assert r1.status_code == 200
    r2 = _plan(client, "swap", 2, [1, 0], [0, 0], horizon=1)
    assert r2.status_code == 409
    ev = r2.json()["error"]["details"]["evidence"]
    assert ev["type"] == "NO_PATH_IN_HORIZON"
    assert ev["root_cause"] == "EDGE_CONFLICT_SWAP"
    assert ev["from"] == [1, 0]
    assert ev["to"] == [0, 0]
    assert ev["with_robot"] == 1


# --------------------------------------------------------------- 验收 2：终点占用
def test_endpoint_occupation(client):
    _create_map(client, "ep", 4, 1)
    _plan(client, "ep", 1, [0, 0], [2, 0])
    r = _plan(client, "ep", 2, [3, 0], [2, 0])
    assert r.status_code == 409
    details = r.json()["error"]["details"]
    assert details["error_subtype"] == "ENDPOINT_OCCUPIED"
    assert details["evidence"]["cell"] == [2, 0]


# --------------------------------------------------------------- 验收 3：撤销预订
def test_cancel_with_token_then_replan_succeeds(client):
    _create_map(client, "c", 5, 1)
    r1 = _plan(client, "c", 1, [0, 0], [4, 0])
    token = r1.json()["cancel_token"]
    rid = r1.json()["reservation_id"]

    # 撤销前 r2 走不通
    r2_before = _plan(client, "c", 2, [4, 0], [0, 0])
    assert r2_before.status_code == 409

    # 错误凭证 -> 403
    bad = client.post(
        "/api/maps/c/reservations/cancel",
        json={"robot_id": 1, "cancel_token": "a" * 64},
    )
    assert bad.status_code == 403

    r = client.post(
        "/api/maps/c/reservations/cancel",
        json={"robot_id": 1, "cancel_token": token},
    )
    assert r.status_code == 200
    assert r.json()["cancelled"] is True
    assert r.json()["reservation_id"] == rid
    assert r.json()["reservation_version"] == 2

    # 撤销后列表为空
    lst = client.get("/api/maps/c/reservations").json()
    assert lst["reservations"] == []
    assert lst["reservation_version"] == 2

    # r2 现在可以规划成功
    r2 = _plan(client, "c", 2, [4, 0], [0, 0])
    assert r2.status_code == 200, r2.text

    # 重复撤销：没有激活预订 -> 404
    again = client.post(
        "/api/maps/c/reservations/cancel",
        json={"robot_id": 1, "cancel_token": token},
    )
    assert again.status_code == 404


def test_replan_replaces_existing_reservation(client):
    _create_map(client, "r", 5, 2)
    a = _plan(client, "r", 1, [0, 0], [4, 0])
    assert a.status_code == 200
    b = _plan(client, "r", 1, [0, 0], [4, 1])
    assert b.status_code == 200
    lst = client.get("/api/maps/r/reservations").json()["reservations"]
    assert len(lst) == 1
    assert lst[0]["path"][-1] == [4, 1]


# --------------------------------------------------------------- 版本控制
def test_stale_map_version_returns_mismatch(client):
    _create_map(client, "v", 3, 1)
    client.put("/api/maps/v/obstacles", json={"obstacles": [[1, 0]]})
    # 客户端仍持旧版本号 1
    r = _plan(client, "v", 1, [0, 0], [2, 0], expected_map_version=1)
    assert r.status_code == 409
    d = r.json()["error"]["details"]
    assert d["error_subtype"] == "MAP_VERSION_MISMATCH"
    assert d["current_map_version"] == 2
    assert len(d["content_hash"]) == 64
    # 用新版本后规划照常进行（虽然会因障碍失败，但是是规划失败而非版本失败）
    r2 = _plan(client, "v", 1, [0, 0], [2, 0], expected_map_version=2)
    assert r2.status_code == 409
    assert r2.json()["error"]["details"]["error_subtype"] != (
        "MAP_VERSION_MISMATCH")


def test_stale_reservation_version_returns_conflict(client):
    _create_map(client, "v2", 4, 1)
    _plan(client, "v2", 1, [0, 0], [3, 0])  # res_version -> 1
    r = _plan(
        client, "v2", 2, [3, 0], [0, 0],
        expected_reservation_version=0)
    assert r.status_code == 409
    assert r.json()["error"]["details"]["error_subtype"] == (
        "RESERVATION_VERSION_STALE")


def test_map_change_during_planning_forces_revalidation(client, monkeypatch):
    """规划进行期间地图变化 -> 提交点重新校验，返回 MAP_VERSION_MISMATCH。"""
    from app import service as service_mod

    _create_map(client, "live", 12, 1)

    def hook(map_id):
        if map_id == "live":
            # 在 A* 执行前直接通过另一条服务路径改地图（真实写库+版本递增）
            client.put(
                "/api/maps/live/obstacles",
                json={"obstacles": [[6, 0]]})

    service_mod.pre_search_hook = hook
    r = _plan(
        client, "live", 1, [0, 0], [11, 0],
        expected_map_version=1)
    assert r.status_code == 409
    d = r.json()["error"]["details"]
    assert d["error_subtype"] == "MAP_VERSION_MISMATCH"
    assert d["current_map_version"] == 2


def test_map_content_hash_changes_with_obstacles(client):
    m1 = _create_map(client, "h", 3, 1, [])
    r = client.put("/api/maps/h/obstacles", json={"obstacles": [[1, 0]]})
    m2 = r.json()
    assert m2["content_hash"] != m1["content_hash"]
    assert m2["map_version"] == 2
    r2 = client.put("/api/maps/h/obstacles", json={"obstacles": [[1, 0]]})
    # 内容相同 -> 哈希相同，但版本仍递增（每次 PUT 是一次变更事件）
    assert r2.json()["content_hash"] == m2["content_hash"]


# --------------------------------------------------------------- 验收 4：并发
def test_concurrent_conflicting_requests_one_wins_one_gets_evidence(client):
    """两个对向窄道请求真正并发：恰好一个成功，另一个拿到冲突证据，
    数据库不变体被破坏（无重叠顶点/边）。

    注意：规划服务在锁外搜索 + 提交点重校验，竞争失败方会用最新快照
    重试，因此工作线程内做小轮次重试直到拿到 200/409 为止。
    """
    import time

    _create_map(client, "par", 6, 1)
    outcomes: list = []

    def worker(robot_id, start, goal):
        for _ in range(20):
            r = _plan(client, "par", robot_id, start, goal)
            if r.status_code in (200, 409):
                outcomes.append((robot_id, r.status_code, r.json()))
                return
            time.sleep(0.01)
        outcomes.append((robot_id, 500, {}))

    t1 = threading.Thread(target=worker, args=(1, [0, 0], [5, 0]))
    t2 = threading.Thread(target=worker, args=(2, [5, 0], [0, 0]))
    t1.start(); t2.start(); t1.join(); t2.join()

    assert len(outcomes) == 2, outcomes
    statuses = sorted(o[1] for o in outcomes)
    assert statuses == [200, 409], statuses
    loser = next(o for o in outcomes if o[1] == 409)
    details = loser[2]["error"]["details"]
    assert details["evidence"]["with_robot"] in (1, 2)
    assert details["evidence"]["with_robot"] != loser[0]

    # 数据库不变量：恰好一条激活预订
    lst = client.get("/api/maps/par/reservations").json()
    assert len(lst["reservations"]) == 1


def test_concurrent_independent_requests_both_succeed(client):
    """两个互不相交的并发请求都成功。"""
    _create_map(client, "ind", 8, 2)
    barrier = threading.Barrier(2)
    outcomes = []

    def worker(robot_id, start, goal):
        barrier.wait()
        outcomes.append(_plan(client, "ind", robot_id, start, goal).status_code)

    t1 = threading.Thread(target=worker, args=(1, [0, 0], [7, 0]))
    t2 = threading.Thread(target=worker, args=(2, [0, 1], [7, 1]))
    t1.start(); t2.start(); t1.join(); t2.join()
    assert sorted(outcomes) == [200, 200]
    lst = client.get("/api/maps/ind/reservations").json()
    assert len(lst["reservations"]) == 2


# --------------------------------------------------------------- 批量 + 固定优先级
def test_batch_planning_fixed_priority(client):
    _create_map(client, "b", 3, 2)
    r = client.post(
        "/api/maps/b/reservations/batch",
        json={"requests": [
            {"robot_id": 2, "start": [2, 0], "goal": [0, 0]},
            {"robot_id": 1, "start": [0, 1], "goal": [2, 1]},
        ]})
    assert r.status_code == 200, r.text
    j = r.json()
    assert j["planned"] is True
    # 结果按固定优先级 robot_id 升序
    assert [x["robot_id"] for x in j["results"]] == [1, 2]
    assert len(j["results"]) == 2
    # 批量是原子的：res_version 只 +1
    assert j["reservation_version"] == 1


def test_batch_failure_is_atomic_and_carries_evidence(client):
    """低优先级机器人在固定优先级下无路可走 -> 整批无预订落库。"""
    _create_map(client, "bf", 5, 1)
    r = client.post(
        "/api/maps/bf/reservations/batch",
        json={"requests": [
            {"robot_id": 1, "start": [0, 0], "goal": [4, 0]},
            {"robot_id": 2, "start": [4, 0], "goal": [0, 0]},
        ]})
    assert r.status_code == 409
    d = r.json()["error"]["details"]
    assert d["failed_robot"] == 2
    assert d["evidence"]["with_robot"] == 1
    # 原子性：没有任何预订
    lst = client.get("/api/maps/bf/reservations").json()
    assert lst["reservations"] == []


def test_reservations_have_no_overlapping_vertices_or_edges(client):
    """随机化不变量检查：多条预订之间绝无顶点/边冲突。"""
    import random
    import sqlite3

    db_file = os.environ["STP_DB_PATH"]
    _create_map(client, "grid", 4, 4)
    random.seed(7)
    placed = 0
    for rid in range(1, 9):
        s = [random.randrange(4), random.randrange(4)]
        g = [random.randrange(4), random.randrange(4)]
        r = _plan(client, "grid", rid, s, g)
        if r.status_code == 200:
            placed += 1
    assert placed >= 3  # 至少放下几条

    conn = sqlite3.connect(db_file)
    # 顶点唯一性（同图同刻同格至多一条）
    dup_v = conn.execute(
        "SELECT t, x, y, COUNT(*) FROM reservation_vertex "
        "GROUP BY map_id, t, x, y HAVING COUNT(*) > 1").fetchall()
    assert dup_v == []
    # 边唯一性（同图同 tick 同无向边至多一条）
    dup_e = conn.execute(
        "SELECT t, ax, ay, bx, by, COUNT(*) FROM reservation_edge "
        "GROUP BY map_id, t, ax, ay, bx, by HAVING COUNT(*) > 1").fetchall()
    assert dup_e == []
    # 同图每机器人至多一条激活预订
    dup_r = conn.execute(
        "SELECT map_id, robot_id, COUNT(*) FROM reservations "
        "WHERE status='active' GROUP BY map_id, robot_id "
        "HAVING COUNT(*) > 1").fetchall()
    assert dup_r == []
