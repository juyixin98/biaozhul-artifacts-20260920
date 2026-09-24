"""HTTP-level tests for the FastAPI service."""

import numpy as np
import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.synthetic import make_scene

client = TestClient(app)


def test_health():
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


def test_icp_endpoint_nominal():
    scene = make_scene(n_points=100, kind="volume", angle_deg=15.0,
                       translation=0.3, noise_std=0.01, seed=1)
    body = {
        "source": scene.source.tolist(),
        "target": scene.target.tolist(),
        "max_iterations": 100,
        "robust_quantile": 1.0,
    }
    resp = client.post("/api/icp", json=body)
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "converged"
    R = np.asarray(data["R"])
    assert R.shape == (3, 3)
    assert abs(np.linalg.det(R) - 1.0) < 1e-8
    assert data["rmse"] < 0.05
    assert len(data["t"]) == 3


def test_icp_endpoint_rejects_bad_rotation():
    body = {
        "source": np.zeros((5, 3)).tolist(),
        "target": np.ones((5, 3)).tolist(),
        "R0": (np.eye(3) * 2).tolist(),
    }
    resp = client.post("/api/icp", json=body)
    assert resp.status_code == 422


def test_icp_endpoint_rejects_bad_points():
    body = {
        "source": [[1.0, 2.0]],  # not 3D
        "target": np.ones((5, 3)).tolist(),
    }
    resp = client.post("/api/icp", json=body)
    assert resp.status_code == 422


def test_synthetic_scene_endpoint():
    resp = client.post(
        "/api/synthetic-scene",
        json={"kind": "volume", "angle_deg": 20.0, "noise_std": 0.01,
              "overlap": 0.8, "n_clutter": 10, "seed": 3},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert len(data["source"]) == 120
    assert len(data["target"]) == int(round(0.8 * 120)) + 10
    R = np.asarray(data["R_true"])
    assert abs(np.linalg.det(R) - 1.0) < 1e-9


@pytest.mark.parametrize("scenario", [
    "nominal", "collinear", "partial_overlap", "bad_initial", "nonconverge",
])
def test_all_demo_scenarios(scenario):
    resp = client.post("/api/demo", json={"scenario": scenario, "seed": 0})
    assert resp.status_code == 200
    data = resp.json()
    assert "result" in data
    r = data["result"]
    assert r["status"] in ("converged", "max_iterations", "insufficient_pairs")
    # No endpoint may ever report global optimality.
    joined = " ".join(r["warnings"]).lower()
    assert "global optimum" not in joined.replace("not verified as the global optimum", "")

    if scenario == "nominal":
        assert r["status"] == "converged"
        assert data["rotation_error_deg"] < 1.0
        assert r["degeneracy"]["degenerate"] is False
    if scenario == "collinear":
        assert r["degeneracy"]["degenerate"] is True
        assert r["degeneracy"]["kind"] == "collinear"
        assert data["translation_error"] < 1e-6
    if scenario == "nonconverge":
        assert r["status"] == "max_iterations"
        assert any("did not converge" in w for w in r["warnings"])
