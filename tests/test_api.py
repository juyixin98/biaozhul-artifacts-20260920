"""End-to-end tests of the FastAPI service using its test client."""

import numpy as np
import pytest
from fastapi.testclient import TestClient

from app import create_app

SQRT2_2 = np.sqrt(2.0) / 2.0
Q_Z90 = [0.0, 0.0, SQRT2_2, SQRT2_2]


@pytest.fixture()
def client():
    return TestClient(create_app())


def post(client, parent, child, time, translation, rotation):
    return client.post(
        "/transforms",
        json={
            "parent": parent,
            "child": child,
            "time": time,
            "translation": translation,
            "rotation": rotation,
        },
    )


def test_health(client):
    assert client.get("/health").json() == {"status": "ok"}


def test_post_and_lookup_roundtrip(client):
    assert post(client, "world", "base", 0.0, [1, 2, 3], Q_Z90).status_code == 201
    assert post(client, "base", "camera", 0.0, [0.5, 0, 0], [0, 0, 0, 1]).status_code == 201

    resp = client.get("/lookup", params={"source": "camera", "target": "world", "time": 0.0})
    assert resp.status_code == 200
    body = resp.json()
    # world->base rotates +90 deg about z, so base->camera [0.5,0,0] becomes
    # [0,0.5,0] in world; plus the [1,2,3] offset.
    np.testing.assert_allclose(body["translation"], [1.0, 2.5, 3.0], atol=1e-9)
    np.testing.assert_allclose(np.abs(body["rotation"]), np.abs(Q_Z90), atol=1e-9)


def test_lookup_interpolates(client):
    post(client, "world", "base", 0.0, [0, 0, 0], [0, 0, 0, 1])
    post(client, "world", "base", 2.0, [2, 0, 0], Q_Z90)
    body = client.get(
        "/lookup", params={"source": "base", "target": "world", "time": 1.0}
    ).json()
    np.testing.assert_allclose(body["translation"], [1.0, 0.0, 0.0], atol=1e-9)
    assert 2.0 * np.arccos(min(1.0, abs(body["rotation"][3]))) == pytest.approx(
        np.pi / 4.0, abs=1e-9
    )


def test_lookup_missing_chain_404(client):
    post(client, "a", "b", 0.0, [0, 0, 0], [0, 0, 0, 1])
    resp = client.get("/lookup", params={"source": "b", "target": "zzz", "time": 0.0})
    assert resp.status_code == 404


def test_lookup_extrapolation_416(client):
    post(client, "a", "b", 0.0, [0, 0, 0], [0, 0, 0, 1])
    post(client, "a", "b", 1.0, [1, 0, 0], [0, 0, 0, 1])
    resp = client.get("/lookup", params={"source": "b", "target": "a", "time": 5.0})
    assert resp.status_code == 416


def test_cycle_rejected_409(client):
    post(client, "a", "b", 0.0, [0, 0, 0], [0, 0, 0, 1])
    post(client, "b", "c", 0.0, [0, 0, 0], [0, 0, 0, 1])
    resp = post(client, "c", "a", 0.0, [0, 0, 0], [0, 0, 0, 1])
    assert resp.status_code == 409


def test_invalid_quaternion_422(client):
    resp = post(client, "a", "b", 0.0, [0, 0, 0], [0, 0, 0, 0])
    assert resp.status_code == 422


def test_frames_listing(client):
    post(client, "a", "b", 0.0, [0, 0, 0], [0, 0, 0, 1])
    post(client, "b", "c", 0.0, [0, 0, 0], [0, 0, 0, 1])
    body = client.get("/frames").json()
    assert body["frames"] == ["a", "b", "c"]
    assert body["edges"] == [
        {"parent": "a", "child": "b"},
        {"parent": "b", "child": "c"},
    ]
