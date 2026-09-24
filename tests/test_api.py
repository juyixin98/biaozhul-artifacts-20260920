"""End-to-end tests of the FastAPI service via the in-process test client."""

import math

import pytest

from fastapi.testclient import TestClient

from occupancy_grid.app import create_app


@pytest.fixture()
def client():
    return TestClient(create_app())


def create_grid(client, **kwargs):
    resp = client.post("/grids", json={"nx": 6, "ny": 5, "origin_x": -3.0,
                                       "origin_y": -2.0, **kwargs})
    assert resp.status_code == 201, resp.text
    return resp.json()["grid_id"]


def test_health_and_lifecycle(client):
    assert client.get("/health").json() == {"status": "ok"}
    gid = create_grid(client)
    assert client.get("/grids").json()["grids"][0]["grid_id"] == gid
    assert client.delete(f"/grids/{gid}").status_code == 204
    assert client.get(f"/grids/{gid}").status_code == 404


def test_echo_ray_and_cell_query(client):
    gid = create_grid(client)
    resp = client.post(f"/grids/{gid}/rays", json={
        "ox": -2.5, "oy": -0.5, "ex": 1.5, "ey": -0.5})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["kind"] == "echo"
    assert body["occupied_cell"] == [4, 1]
    assert (0, 1) in [tuple(c) for c in body["free_cells"]]

    cell = client.get(f"/grids/{gid}/cells/4/1").json()
    assert cell["state"] == "occupied"
    assert cell["probability"] == pytest.approx(0.7)
    assert client.get(f"/grids/{gid}/cells/0/1").json()["state"] == "free"
    assert client.get(f"/grids/{gid}/cells/5/4").json()["state"] == "unknown"


def test_no_return_ray(client):
    gid = create_grid(client)
    resp = client.post(f"/grids/{gid}/rays", json={
        "ox": -2.5, "oy": -1.5, "angle": 0.0, "max_range": 3.0})
    assert resp.status_code == 200
    body = resp.json()
    assert body["kind"] == "no_return"
    assert body["occupied_cell"] is None
    for ix in range(4):
        assert client.get(f"/grids/{gid}/cells/{ix}/0").json()["state"] == "free"


def test_echo_outside_map(client):
    gid = create_grid(client)
    resp = client.post(f"/grids/{gid}/rays", json={
        "ox": -2.5, "oy": -1.5, "ex": 50.0, "ey": -1.5})
    assert resp.status_code == 200
    body = resp.json()
    assert body["endpoint_in_map"] is False
    assert body["occupied_cell"] is None


def test_validation_errors(client):
    gid = create_grid(client)
    # Neither endpoint nor direction.
    resp = client.post(f"/grids/{gid}/rays", json={"ox": 0.0, "oy": 0.0})
    assert resp.status_code == 422
    # Only one endpoint coordinate.
    resp = client.post(f"/grids/{gid}/rays", json={"ox": 0.0, "oy": 0.0, "ex": 1.0})
    assert resp.status_code == 422
    # Bad geometry on creation.
    assert client.post("/grids", json={"nx": 0, "ny": 1}).status_code == 422
    # Unknown grid.
    assert client.get("/grids/nope/cells/0/0").status_code == 404
    # Cell outside map.
    assert client.get(f"/grids/{gid}/cells/99/0").status_code == 400


def test_export_import_roundtrip(client):
    gid = create_grid(client)
    client.post(f"/grids/{gid}/rays", json={
        "ox": -2.5, "oy": -0.5, "ex": 1.5, "ey": -0.5})
    export = client.get(f"/grids/{gid}/export").json()

    resp = client.post("/grids/import", json=export)
    assert resp.status_code == 200, resp.text
    gid2 = resp.json()["grid_id"]
    assert client.get(f"/grids/{gid2}/states").json() == \
        client.get(f"/grids/{gid}/states").json()
    assert client.get(f"/grids/{gid2}/cells/4/1").json()["state"] == "occupied"


def test_import_rejects_garbage(client):
    assert client.post("/grids/import", json={"schema_version": 1}).status_code == 422


def test_grid_summary_counts(client):
    gid = create_grid(client)
    client.post(f"/grids/{gid}/rays", json={
        "ox": -2.5, "oy": -0.5, "ex": 0.5, "ey": -0.5})
    summary = client.get(f"/grids/{gid}").json()
    assert summary["counts"]["occupied"] == 1
    assert summary["counts"]["free"] == 3
    assert summary["counts"]["unknown"] == 30 - 4
