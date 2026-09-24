"""HTTP API tests using FastAPI's TestClient."""

import numpy as np
import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.rotation import geodesic_angle, rotvec_to_quat

client = TestClient(app)


def test_health():
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


def test_average_endpoint_basic():
    payload = {
        "quaternions": [
            [1.0, 0.0, 0.0, 0.0],
            [0.9998, 0.0, 0.0, 0.02],
            [0.9998, 0.0, 0.0, -0.02],
        ],
        "weights": [1.0, 2.0, 2.0],
    }
    resp = client.post("/api/v1/average", json=payload)
    assert resp.status_code == 200
    body = resp.json()
    assert body["converged"]
    assert len(body["quaternion"]) == 4
    assert len(body["rotation_matrix"]) == 3
    assert len(body["geodesic_residuals_rad"]) == 3
    assert len(body["robust_weights"]) == 3
    assert body["multi_solution_hint"] is False

    r = np.array(body["rotation_matrix"])
    assert np.allclose(r @ r.T, np.eye(3), atol=1e-9)
    assert np.linalg.det(r) == pytest.approx(1.0, abs=1e-9)


def test_average_endpoint_sign_equivalence():
    q = rotvec_to_quat(np.array([0.0, 0.0, 0.5])).tolist()
    q_neg = (-np.array(q)).tolist()
    r1 = client.post("/api/v1/average",
                     json={"quaternions": [q, q_neg]}).json()
    r2 = client.post("/api/v1/average",
                     json={"quaternions": [q_neg, q]}).json()
    assert geodesic_angle(np.array(r1["quaternion"]),
                          np.array(r2["quaternion"])) < 1e-10


def test_average_endpoint_outlier_flagged():
    inliers = [[1.0, 0.0, 0.0, 0.0]] * 5
    outlier = rotvec_to_quat(np.array([np.pi / 2, 0.0, 0.0])).tolist()
    resp = client.post("/api/v1/average",
                       json={"quaternions": inliers + [outlier]})
    body = resp.json()
    assert body["outlier_indices"] == [5]


def test_average_endpoint_multi_solution_hint():
    payload = {"quaternions": [[1.0, 0, 0, 0], [0.0, 1.0, 0, 0]]}
    body = client.post("/api/v1/average", json=payload).json()
    assert body["multi_solution_hint"] is True


def test_validation_error_zero_norm():
    resp = client.post("/api/v1/average",
                       json={"quaternions": [[0.0, 0.0, 0.0, 0.0]]})
    assert resp.status_code == 422


def test_validation_error_bad_shape():
    resp = client.post("/api/v1/average",
                       json={"quaternions": [[1.0, 0.0, 0.0]]})
    assert resp.status_code == 422


def test_validation_error_weight_mismatch():
    resp = client.post("/api/v1/average", json={
        "quaternions": [[1.0, 0, 0, 0], [1.0, 0, 0, 0]],
        "weights": [1.0],
    })
    assert resp.status_code == 422
