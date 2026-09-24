"""End-to-end HTTP tests: snapshot binding, incremental endpoints, crypto.

Uses FastAPI's in-process ASGI transport (httpx) -- no network sockets.
"""

import json

import numpy as np
import pytest
from fastapi.testclient import TestClient

from app.main import app, store
from app import crypto


@pytest.fixture
def client():
    store._sessions.clear()
    with TestClient(app) as c:
        yield c


def _open_grid(client, w=9, h=5, blocked_cols=(), connectivity=8):
    blocked = [[False] * w for _ in range(h)]
    for col in blocked_cols:
        for y in range(h):
            blocked[y][col] = True
    resp = client.post(
        "/maps",
        json={
            "width": w,
            "height": h,
            "blocked": blocked,
            "start": [0, h // 2],
            "goal": [w - 1, h // 2],
            "connectivity": connectivity,
        },
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


# ---------------------------------------------------------------------- #
# Happy path + optimal-match reporting
# ---------------------------------------------------------------------- #
def test_create_and_plan_open_map(client):
    m = _open_grid(client)
    resp = client.post(
        f"/maps/{m['map_id']}/plan",
        json={"map_id": m["map_id"], "expected_snapshot_id": m["snapshot_id"]},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["reachable"] is True
    assert body["optimal_match"] is True
    assert body["cost"] == body["dijkstra_cost"]
    assert body["path"][0] == [0, 2]
    assert body["path"][-1] == [8, 2]
    assert body["path_valid"] is True
    assert abs(body["path_cost_check"] - body["cost"]) < 1e-9
    # diagnostic fields present
    assert isinstance(body["reexpanded_nodes"], int)
    assert isinstance(body["dijkstra_settled_nodes"], int)
    assert "signature" in body and body["signature"]


def test_wall_makes_goal_unreachable(client):
    m = _open_grid(client, w=5, h=3, blocked_cols=(2,))
    resp = client.post(
        f"/maps/{m['map_id']}/plan",
        json={"map_id": m["map_id"], "expected_snapshot_id": m["snapshot_id"]},
    )
    body = resp.json()
    assert body["reachable"] is False
    assert body["cost"] is None
    assert body["path"] is None
    assert body["dijkstra_cost"] is None
    assert body["optimal_match"] is True  # both agree: unreachable


def test_obstacle_addition_then_removal(client):
    m = _open_grid(client, w=7, h=3)
    mid = m["map_id"]
    snap = m["snapshot_id"]

    def plan(snapshot):
        return client.post(
            f"/maps/{mid}/plan", json={"map_id": mid, "expected_snapshot_id": snapshot}
        ).json()

    first = plan(snap)
    assert first["reachable"]

    # add a full wall of obstacles
    resp = client.post(
        f"/maps/{mid}/costs",
        json={
            "map_id": mid,
            "expected_snapshot_id": snap,
            "updates": [{"x": 3, "y": y, "blocked": True} for y in range(3)],
        },
    )
    assert resp.status_code == 200, resp.text
    blocked_body = resp.json()
    assert blocked_body["map_changed"] is True
    assert blocked_body["snapshot_id"] != snap
    assert blocked_body["reachable"] is False
    assert blocked_body["optimal_match"] is True

    # remove one obstacle: path repaired incrementally, cost matches Dijkstra
    snap2 = blocked_body["snapshot_id"]
    resp = client.post(
        f"/maps/{mid}/costs",
        json={
            "map_id": mid,
            "expected_snapshot_id": snap2,
            "updates": [{"x": 3, "y": 1, "blocked": False, "cost": 1.0}],
        },
    )
    reopened = resp.json()
    assert reopened["reachable"] is True
    assert reopened["optimal_match"] is True
    assert reopened["cost"] == reopened["dijkstra_cost"]


def test_move_start_endpoint(client):
    m = _open_grid(client)
    mid, snap = m["map_id"], m["snapshot_id"]
    resp = client.post(
        f"/maps/{mid}/move-start",
        json={
            "map_id": mid,
            "expected_snapshot_id": snap,
            "start": [0, 0],
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["start"] == [0, 0]
    assert body["optimal_match"] is True
    assert body["path"][0] == [0, 0]
    assert body["snapshot_id"] != snap  # snapshot tracks geometry too


# ---------------------------------------------------------------------- #
# Snapshot binding: stale state must not be silently reused
# ---------------------------------------------------------------------- #
def test_stale_snapshot_rejected_with_409(client):
    m = _open_grid(client)
    mid, old_snap = m["map_id"], m["snapshot_id"]
    # advance the map
    upd = client.post(
        f"/maps/{mid}/costs",
        json={
            "map_id": mid,
            "expected_snapshot_id": old_snap,
            "updates": [{"x": 1, "y": 1, "cost": 5.0}],
        },
    )
    assert upd.status_code == 200
    new_snap = upd.json()["snapshot_id"]

    # planning with the OLD snapshot is a conflict, not a reused old path
    resp = client.post(
        f"/maps/{mid}/plan",
        json={"map_id": mid, "expected_snapshot_id": old_snap},
    )
    assert resp.status_code == 409
    assert resp.json()["error"] == "snapshot_mismatch"

    # chained update on stale snapshot also rejected
    resp = client.post(
        f"/maps/{mid}/costs",
        json={
            "map_id": mid,
            "expected_snapshot_id": old_snap,
            "updates": [{"x": 2, "y": 2, "cost": 2.0}],
        },
    )
    assert resp.status_code == 409

    # current snapshot works
    ok = client.post(
        f"/maps/{mid}/plan", json={"map_id": mid, "expected_snapshot_id": new_snap}
    )
    assert ok.status_code == 200
    assert ok.json()["optimal_match"] is True


def test_snapshot_id_changes_with_content_only(client):
    m1 = _open_grid(client)
    m2 = _open_grid(client)
    # identical maps -> identical content digests
    assert m1["snapshot_id"] == m2["snapshot_id"]


def test_path_from_old_snapshot_not_reused(client):
    """Obstacle added on the old optimal path: new plan must avoid it."""
    m = _open_grid(client, w=9, h=5)
    mid, snap = m["map_id"], m["snapshot_id"]
    first = client.post(
        f"/maps/{mid}/plan", json={"map_id": mid, "expected_snapshot_id": snap}
    ).json()
    assert first["path"]
    # block a cell on the path
    on_path = first["path"][3]
    upd = client.post(
        f"/maps/{mid}/costs",
        json={
            "map_id": mid,
            "expected_snapshot_id": snap,
            "updates": [{"x": on_path[0], "y": on_path[1], "blocked": True}],
        },
    ).json()
    assert upd["optimal_match"] is True
    for p in upd["path"]:
        assert p != on_path


# ---------------------------------------------------------------------- #
# Validation
# ---------------------------------------------------------------------- #
def test_negative_cost_rejected_over_http(client):
    resp = client.post(
        "/maps",
        json={
            "width": 2,
            "height": 2,
            "cost": [[1.0, -3.0], [1.0, 1.0]],
            "start": [0, 0],
            "goal": [1, 1],
        },
    )
    assert resp.status_code == 422


def test_blocking_start_rejected(client):
    m = _open_grid(client)
    mid, snap = m["map_id"], m["snapshot_id"]
    sy = m["start"][1]
    resp = client.post(
        f"/maps/{mid}/costs",
        json={
            "map_id": mid,
            "expected_snapshot_id": snap,
            "updates": [{"x": 0, "y": sy, "blocked": True}],
        },
    )
    assert resp.status_code == 422


def test_unknown_map_404(client):
    assert client.post("/maps/nope/plan", json={"map_id": "nope"}).status_code == 404


def test_delete_map(client):
    m = _open_grid(client)
    r = client.delete(f"/maps/{m['map_id']}")
    assert r.status_code == 200 and r.json()["deleted"] is True
    assert client.post(
        f"/maps/{m['map_id']}/plan", json={"map_id": m["map_id"]}
    ).status_code == 404


# ---------------------------------------------------------------------- #
# Cryptography: real HMAC verification round-trip + tamper detection
# ---------------------------------------------------------------------- #
def test_signature_roundtrip_and_tamper_detection(client):
    m = _open_grid(client)
    mid, snap = m["map_id"], m["snapshot_id"]
    resp = client.post(
        f"/maps/{mid}/plan", json={"map_id": mid, "expected_snapshot_id": snap}
    )
    body = resp.json()
    sig = body.pop("signature")

    # verify exactly what the server signed
    ok = client.post(
        f"/maps/{mid}/verify",
        json={"map_id": mid, "snapshot_id": snap, "body": body, "signature": sig},
    ).json()
    assert ok["valid"] is True

    # tamper with the cost -> verification must fail
    tampered = dict(body)
    tampered["cost"] = (body["cost"] or 0) + 1.0
    bad = client.post(
        f"/maps/{mid}/verify",
        json={"map_id": mid, "snapshot_id": snap, "body": tampered, "signature": sig},
    ).json()
    assert bad["valid"] is False

    # forge a signature with a different random key -> fails
    forged = crypto.sign_response(crypto.new_hmac_key(),
                                  json.dumps(body, sort_keys=True, separators=(",", ":")).encode(),
                                  snap)
    bad2 = client.post(
        f"/maps/{mid}/verify",
        json={"map_id": mid, "snapshot_id": snap, "body": body, "signature": forged},
    ).json()
    assert bad2["valid"] is False


def test_snapshot_digest_is_real_sha256(client):
    """The digest must equal an independent SHA-256 over the canonical state."""
    w, h = 3, 2
    cost = [[1.0, 2.0, 3.0], [4.0, 5.0, 6.0]]
    blocked = [[False, True, False], [False, False, False]]
    resp = client.post(
        "/maps",
        json={
            "width": w,
            "height": h,
            "cost": cost,
            "blocked": blocked,
            "start": [0, 0],
            "goal": [2, 1],
            "connectivity": 8,
        },
    )
    assert resp.status_code == 201, resp.text
    payload = crypto.canonical_snapshot_payload(
        width=w, height=h,
        cost=np.asarray(cost), blocked=np.asarray(blocked),
        connectivity=8, diagonal_rule="two_blocked",
        start=(0, 0), goal=(2, 1),
    )
    assert resp.json()["snapshot_id"] == crypto.snapshot_digest(payload)
    assert len(resp.json()["snapshot_id"]) == 64  # sha256 hex


def test_hmac_key_unique_and_32_bytes(client):
    keys = {_open_grid(client)["hmac_key"] for _ in range(3)}
    assert len(keys) == 3
    for k in keys:
        assert len(bytes.fromhex(k)) == 32
