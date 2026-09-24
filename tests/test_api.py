"""FastAPI HTTP 端到端测试 (TestClient/httpx)。"""

import math

import pytest
from fastapi.testclient import TestClient

from app.api import app, service

client = TestClient(app)


@pytest.fixture(autouse=True)
def _fresh_service():
    # 每个用例使用干净的内存注册表
    service.maps.clear()
    service.planners.clear()
    yield
    service.maps.clear()
    service.planners.clear()


def _make_map(rows=6, cols=6, obstacles=None, connectivity=8, weights=None):
    payload = {
        "rows": rows,
        "cols": cols,
        "connectivity": connectivity,
        "obstacles": [{"row": r, "col": c} for r, c in (obstacles or [])],
    }
    if weights is not None:
        payload["weights"] = weights
    r = client.post("/api/maps", json=payload)
    assert r.status_code == 200, r.text
    return r.json()


def _make_planner(map_id, start, goal, snapshot_id=None):
    payload = {
        "map_id": map_id,
        "start": {"row": start[0], "col": start[1]},
        "goal": {"row": goal[0], "col": goal[1]},
    }
    if snapshot_id:
        payload["snapshot_id"] = snapshot_id
    r = client.post("/api/planners", json=payload)
    assert r.status_code == 200, r.text
    return r.json()["planner_id"]


def test_health():
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_full_lifecycle_open_grid():
    mp = _make_map(6, 6)
    pid = _make_planner(mp["map_id"], (0, 0), (5, 5))
    r = client.post(f"/api/planners/{pid}/plan", json={})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True
    assert body["reachable"] is True
    assert body["cost"] == pytest.approx(5 * math.sqrt(2.0), rel=1e-9)
    assert body["path"][0] == [0, 0]
    assert body["path"][-1] == [5, 5]
    assert body["path_valid"] is True
    # 每次都与独立 Dijkstra 对拍
    assert body["benchmark"]["cost_match"] is True
    assert body["benchmark"]["dijkstra_cost"] == pytest.approx(body["cost"], rel=1e-9)
    # 诊断字段存在(观测值, 非承诺)
    assert "expanded_nonstale" in body["diagnostics"]


def test_map_update_then_planner_update_incremental():
    mp = _make_map(6, 6)
    pid = _make_planner(mp["map_id"], (0, 0), (5, 5))
    before = client.post(f"/api/planners/{pid}/plan", json={}).json()["cost"]

    r = client.post(
        f"/api/planners/{pid}/update-map",
        json={"changes": [{"row": 2, "col": 2, "kind": "block"}]},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["map_changed"] is True
    assert body["snapshot_id"] != mp["snapshot"]["snapshot_id"]
    assert body["snapshot_version"] == 1
    assert body["cost"] > before
    assert body["benchmark"]["cost_match"] is True

    # 规划器信息已绑定新快照
    info = client.get(f"/api/planners/{pid}").json()
    assert info["snapshot"]["version"] == 1


def test_obstacle_added_then_removed_returns_to_optimal():
    mp = _make_map(6, 6)
    pid = _make_planner(mp["map_id"], (0, 0), (5, 5))
    base = client.post(f"/api/planners/{pid}/plan", json={}).json()["cost"]

    client.post(
        f"/api/planners/{pid}/update-map",
        json={"changes": [{"row": i, "col": 2, "kind": "block"} for i in range(1, 5)]},
    )
    blocked_body = client.post(f"/api/planners/{pid}/plan", json={}).json()
    assert blocked_body["cost"] > base

    client.post(
        f"/api/planners/{pid}/update-map",
        json={"changes": [{"row": i, "col": 2, "kind": "free"} for i in range(1, 5)]},
    )
    after = client.post(f"/api/planners/{pid}/plan", json={}).json()
    assert after["cost"] == pytest.approx(base, abs=1e-9)
    assert after["benchmark"]["cost_match"] is True


def test_unreachable_reports_null_not_stale_path():
    mp = _make_map(5, 5)
    pid = _make_planner(mp["map_id"], (0, 2), (4, 2))
    r = client.post(
        f"/api/planners/{pid}/update-map",
        json={"changes": [{"row": 2, "col": c, "kind": "block"} for c in range(5)]},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["reachable"] is False
    assert body["cost"] is None
    assert body["path"] is None
    assert body["path_valid"] is True  # 无路径与 Dijkstra 不可达一致
    assert body["benchmark"]["dijkstra_cost"] is None


def test_move_start_endpoint():
    mp = _make_map(6, 6, obstacles=[(2, c) for c in range(1, 6)])
    pid = _make_planner(mp["map_id"], (0, 0), (5, 5))
    r = client.post(
        f"/api/planners/{pid}/move-start", json={"start": {"row": 0, "col": 5}}
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True
    assert body["benchmark"]["cost_match"] is True
    assert body["path"][0] == [0, 5]


def test_move_start_onto_obstacle_422():
    mp = _make_map(4, 4, obstacles=[(0, 3)])
    pid = _make_planner(mp["map_id"], (0, 0), (3, 3))
    r = client.post(
        f"/api/planners/{pid}/move-start", json={"start": {"row": 0, "col": 3}}
    )
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "start_blocked"


def test_negative_weight_rejected_on_create():
    r = client.post(
        "/api/maps",
        json={"rows": 2, "cols": 2, "weights": [[1.0, -1.0], [1.0, 1.0]]},
    )
    assert r.status_code in (400, 422)
    assert "weight" in r.text.lower() or "权重" in r.text


def test_negative_weight_change_rejected_atomically():
    mp = _make_map(3, 3)
    r = client.post(
        f"/api/maps/{mp['map_id']}/updates",
        json={"changes": [{"row": 0, "col": 0, "kind": "weight", "weight": -2.0}]},
    )
    # Pydantic 层(422)或服务层(400)都会拒绝
    assert r.status_code in (400, 422)
    # 地图链头版本不变
    info = client.get(f"/api/maps/{mp['map_id']}").json()
    assert info["head"]["version"] == 0


def test_service_layer_negative_weight_is_400():
    # 绕过 Pydantic 直接调用服务层, 验证服务自身也拒绝负代价
    mp = _make_map(3, 3)
    with pytest.raises(Exception) as ei:
        service.update_map(
            mp["map_id"],
            [{"row": 0, "col": 0, "kind": "weight", "weight": -2.0}],
            None,
        )
    assert ei.value.status == 400


def test_snapshot_conflict_on_map_update():
    mp = _make_map(3, 3)
    # 先制造版本 1
    client.post(
        f"/api/maps/{mp['map_id']}/updates",
        json={"changes": [{"row": 0, "col": 0, "kind": "block"}]},
    )
    # 用陈旧的 genesis id 作为期望版本
    r = client.post(
        f"/api/maps/{mp['map_id']}/updates",
        json={
            "changes": [{"row": 1, "col": 1, "kind": "block"}],
            "expected_snapshot": mp["snapshot"]["snapshot_id"],
        },
    )
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "snapshot_conflict"


def test_planner_update_requires_bound_snapshot():
    mp = _make_map(4, 4)
    pid = _make_planner(mp["map_id"], (0, 0), (3, 3))
    r = client.post(
        f"/api/planners/{pid}/update-map",
        json={
            "changes": [{"row": 1, "col": 1, "kind": "block"}],
            "expected_snapshot": "0" * 64,
        },
    )
    assert r.status_code == 409


def test_endpoint_cannot_be_blocked_via_planner_update():
    mp = _make_map(3, 3)
    pid = _make_planner(mp["map_id"], (0, 0), (2, 2))
    # 堵目标
    r = client.post(
        f"/api/planners/{pid}/update-map",
        json={"changes": [{"row": 2, "col": 2, "kind": "block"}]},
    )
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "endpoint_blocked"
    # 堵当前起点
    r = client.post(
        f"/api/planners/{pid}/update-map",
        json={"changes": [{"row": 0, "col": 0, "kind": "block"}]},
    )
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "endpoint_blocked"
    # 快照链没有推进
    info = client.get(f"/api/maps/{mp['map_id']}").json()
    assert info["head"]["version"] == 0


def test_map_verify_chain_endpoint():
    mp = _make_map(3, 3)
    client.post(
        f"/api/maps/{mp['map_id']}/updates",
        json={"changes": [{"row": 0, "col": 0, "kind": "block"}]},
    )
    r = client.get(f"/api/maps/{mp['map_id']}/verify")
    assert r.status_code == 200
    assert r.json()["ok"] is True
    assert len(r.json()["snapshots"]) == 2


def test_reset_planner_discards_incremental_state():
    mp = _make_map(5, 5)
    pid = _make_planner(mp["map_id"], (0, 0), (4, 4))
    # 多次增量更新后重置
    client.post(
        f"/api/planners/{pid}/update-map",
        json={"changes": [{"row": 2, "col": 2, "kind": "block"}]},
    )
    r = client.post(f"/api/planners/{pid}/reset", json={})
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["diagnostics"]["full_reset"] is True
    assert body["benchmark"]["cost_match"] is True


def test_planner_against_named_snapshot():
    mp = _make_map(3, 3)
    # 地图推进到版本 1, 再针对 genesis 创建规划器(历史快照)
    client.post(
        f"/api/maps/{mp['map_id']}/updates",
        json={"changes": [{"row": 0, "col": 0, "kind": "block"}]},
    )
    r = client.post(
        "/api/planners",
        json={
            "map_id": mp["map_id"],
            "start": {"row": 1, "col": 0},
            "goal": {"row": 2, "col": 2},
            "snapshot_id": mp["snapshot"]["snapshot_id"],
        },
    )
    assert r.status_code == 200
    assert r.json()["snapshot"]["version"] == 0


def test_unknown_map_and_planner_404():
    assert client.get("/api/maps/nope").status_code == 404
    assert client.post("/api/planners/nope/plan", json={}).status_code == 404


def test_validation_error_on_bad_payload():
    r = client.post("/api/maps", json={"rows": 0, "cols": 3})
    assert r.status_code == 422


def test_connectivity4_end_to_end():
    mp = _make_map(4, 4, connectivity=4)
    pid = _make_planner(mp["map_id"], (0, 0), (3, 3))
    body = client.post(f"/api/planners/{pid}/plan", json={}).json()
    assert body["cost"] == pytest.approx(6.0, rel=1e-9)
    # 路径中无对角步
    for a, z in zip(body["path"], body["path"][1:]):
        assert abs(a[0] - z[0]) + abs(a[1] - z[1]) == 1


def test_weight_change_end_to_end():
    mp = _make_map(3, 3)
    pid = _make_planner(mp["map_id"], (0, 0), (2, 2))
    r = client.post(
        f"/api/planners/{pid}/update-map",
        json={"changes": [{"row": 1, "col": 1, "kind": "weight", "weight": 50.0}]},
    )
    assert r.status_code == 200
    body = r.json()
    # 抬高中间单元后最优路径不再走 (1,1): 1 斜 + 2 直 = 2 + sqrt(2)
    # (斜行夹角中的高权单元仅"可通行"而非障碍, 故斜行仍合法)
    assert body["cost"] == pytest.approx(2.0 + math.sqrt(2.0), rel=1e-9)
    assert body["cost"] > 2.0 * math.sqrt(2.0)
    assert body["benchmark"]["cost_match"] is True
