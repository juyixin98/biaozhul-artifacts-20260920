"""Restart: state persists on disk; a fresh app instance reproduces results."""
import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from tests.conftest import PARAM_DIR

SAMPLES = [
    {"t_s": float(t), "current_a": 10.0 if t < 50 else 0.0, "voltage_v": 3.78, "temp_c": 25.0}
    for t in range(0, 200)
]


def test_restart_restores_session(data_dir):
    app1 = create_app(data_dir=data_dir, param_dir=PARAM_DIR)
    with TestClient(app1) as c1:
        c1.post("/v1/sessions", json={"session_id": "rk", "initial_soc": 0.7})
        r = c1.post("/v1/sessions/rk/samples", json={"samples": SAMPLES})
        assert r.status_code == 200
        soc_before = c1.get("/v1/sessions/rk/soc").json()

    # simulate process restart: brand-new app on the same data dir
    app2 = create_app(data_dir=data_dir, param_dir=PARAM_DIR)
    with TestClient(app2) as c2:
        assert "rk" in c2.get("/v1/sessions").json()["sessions"]
        soc_after = c2.get("/v1/sessions/rk/soc").json()
        assert soc_after["soc"] == pytest.approx(soc_before["soc"], abs=1e-15)
        assert soc_after["sigma"] == pytest.approx(soc_before["sigma"], abs=1e-15)
        # anchor verification passes after reload from disk
        assert c2.get("/v1/sessions/rk").json()["anchor_verified"] is True
        # evidence chain survived the restart intact
        assert c2.get("/v1/sessions/rk/evidence").json()["chain_valid"] is True
        # and ingestion continues seamlessly
        r = c2.post("/v1/sessions/rk/samples", json={"samples": [
            {"t_s": 200.0, "current_a": 0.0, "voltage_v": 3.78, "temp_c": 25.0}
        ]})
        assert r.status_code == 200 and r.json()["accepted"] == 1
