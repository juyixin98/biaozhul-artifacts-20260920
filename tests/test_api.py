"""API tests using FastAPI's TestClient (in-process, no network)."""

import numpy as np
from fastapi.testclient import TestClient

from occupancy_grid.api import app

client = TestClient(app)


def create_grid(**overrides):
    payload = {
        "width": 5,
        "height": 5,
        "resolution": 1.0,
        "origin_x": 0.0,
        "origin_y": 0.0,
        "l_occ": 1.0,
        "l_free": -0.5,
        "l_min": -10.0,
        "l_max": 10.0,
    }
    payload.update(overrides)
    r = client.post("/grids", json=payload)
    assert r.status_code == 201, r.text
    return r.json()["grid_id"]


def test_create_and_get_grid():
    gid = create_grid()
    r = client.get(f"/grids/{gid}")
    assert r.status_code == 200
    assert r.json()["width"] == 5
    assert client.get("/grids/nonexistent").status_code == 404


def test_rays_endpoint_matches_library():
    gid = create_grid()
    r = client.post(
        f"/grids/{gid}/rays",
        json={"rays": [
            {"sensor_x": 0.5, "sensor_y": 0.5, "end_x": 3.5, "end_y": 0.5, "hit": True}
        ]},
    )
    assert r.status_code == 200
    assert r.json()["cells_updated"] == 4

    cell = client.get(f"/grids/{gid}/cell", params={"ix": 3, "iy": 0}).json()
    assert cell["log_odds"] == 1.0
    assert cell["state"] == "OCCUPIED"
    cell = client.get(f"/grids/{gid}/cell", params={"ix": 0, "iy": 0}).json()
    assert cell["log_odds"] == -0.5
    assert cell["state"] == "FREE"
    cell = client.get(f"/grids/{gid}/cell", params={"ix": 4, "iy": 4}).json()
    assert cell["state"] == "UNKNOWN"


def test_scan_endpoint_and_miss():
    gid = create_grid()
    r = client.post(
        f"/grids/{gid}/scan",
        json={
            "pose_x": 0.5, "pose_y": 0.5, "pose_theta": 0.0,
            "angles": [0.0], "ranges": [99.0], "max_range": 3.0,
        },
    )
    assert r.status_code == 200
    body = client.get(f"/grids/{gid}/log_odds").json()
    lo = np.array(body["log_odds"])
    assert lo.shape == (5, 5)
    assert (lo[0, :4] == -0.5).all()
    assert (lo <= 0).all()  # miss: nothing occupied


def test_scan_rejects_mismatched_lengths():
    gid = create_grid()
    r = client.post(
        f"/grids/{gid}/scan",
        json={
            "pose_x": 0.5, "pose_y": 0.5,
            "angles": [0.0, 1.0], "ranges": [1.0], "max_range": 3.0,
        },
    )
    assert r.status_code == 422


def test_cell_out_of_bounds_404():
    gid = create_grid()
    assert client.get(f"/grids/{gid}/cell", params={"ix": 9, "iy": 0}).status_code == 404


def test_save_load_roundtrip_via_api(tmp_path):
    gid = create_grid(origin_x=-2.5, origin_y=-2.5)
    client.post(
        f"/grids/{gid}/rays",
        json={"rays": [
            {"sensor_x": -1.5, "sensor_y": -1.5, "end_x": 1.5, "end_y": -1.5, "hit": True}
        ]},
    )
    before = np.array(client.get(f"/grids/{gid}/log_odds").json()["log_odds"])

    path = str(tmp_path / "map.npz")
    r = client.post(f"/grids/{gid}/save", json={"path": path})
    assert r.status_code == 200

    r = client.post("/grids/load", json={"path": path})
    assert r.status_code == 201
    gid2 = r.json()["grid_id"]
    assert gid2 != gid
    after = np.array(client.get(f"/grids/{gid2}/log_odds").json()["log_odds"])
    np.testing.assert_array_equal(before, after)


def test_load_missing_file_404():
    assert client.post("/grids/load", json={"path": "/no/such/file.npz"}).status_code == 404


def test_probabilities_endpoint():
    gid = create_grid()
    body = client.get(f"/grids/{gid}/probabilities").json()
    p = np.array(body["probabilities"])
    assert p.shape == (5, 5)
    assert (p == 0.5).all()  # fresh grid: everything unknown
