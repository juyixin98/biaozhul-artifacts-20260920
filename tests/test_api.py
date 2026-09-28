"""HTTP-level tests: identity correlation, envelope, ledger and log trail."""
from __future__ import annotations

import json

from fastapi.testclient import TestClient

from app.main import create_app
from app.reproducibility.fixtures import build_fixture


def _client():
    return TestClient(create_app())


def test_health_and_fixture_catalogue():
    with _client() as c:
        r = c.get("/api/v1/health")
        assert r.status_code == 200
        assert r.json()["core_version"].startswith("did-core")
        r = c.get("/api/v1/fixtures")
        names = r.json()["fixtures"]
        assert "hand_2x2" in names and "contaminated_control" in names
        r = c.get("/api/v1/fixtures/hand_2x2")
        body = r.json()
        assert body["provenance"]["fixture_fingerprint"]
        assert len(body["observations"]) == 8


def test_did_endpoint_ok_envelope_and_ledger(isolated_runtime):
    app = create_app()
    with TestClient(app) as c:
        payload = {"request_id": "http-1", "observations": [o.model_dump() for o in build_fixture("hand_2x2")]}
        r = c.post("/api/v1/did", json=payload)
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["request_id"] == "http-1"
        assert body["status"] == "ok"
        assert abs(body["estimate"]["value"] - 3.0) < 1e-9
        assert body["decomposition"]["did"] == 3.0
        # Identity is traceable through the ledger.
        run = c.get("/api/v1/runs/http-1").json()
        assert run["status"] == "ok"
        assert abs(run["point_estimate"] - 3.0) < 1e-9
        assert run["n_clusters"] == 4
        assert any(s["step"] == "identity_align" for s in run["steps"])
    # And through the structured log.
    lines = [json.loads(l) for l in isolated_runtime["log_path"].read_text().splitlines() if l.strip()]
    assert any(x.get("request_id") == "http-1" and x["event"] == "did_request_ok" for x in lines)


def test_did_endpoint_contamination_422_with_category(isolated_runtime):
    app = create_app()
    with TestClient(app) as c:
        payload = {"request_id": "http-2",
                   "observations": [o.model_dump() for o in build_fixture("contaminated_control")],
                   "pre_period": 0, "post_period": 2}
        r = c.post("/api/v1/did", json=payload)
        assert r.status_code == 422
        body = r.json()
        assert body["status"] == "rejected"
        assert body["failure_category"] == "CONTROL_GROUP_CONTAMINATED"
        assert any(e["unit_id"] == "c2" for e in body["excluded"])
        # The rejected run is also ledgered under the same identity.
        run = c.get("/api/v1/runs/http-2").json()
        assert run["status"] == "rejected"
        assert run["failure_category"] == "CONTROL_GROUP_CONTAMINATED"


def test_did_endpoint_bad_input_400():
    app = create_app()
    with TestClient(app) as c:
        # Empty panel -> 400 EMPTY_PANEL.
        r = c.post("/api/v1/did", json={"request_id": "http-3", "observations": []})
        assert r.status_code == 400
        assert r.json()["failure_category"] == "EMPTY_PANEL"


def test_event_study_endpoint_ok_and_support_refusal(isolated_runtime):
    app = create_app()
    with TestClient(app) as c:
        ok = {"request_id": "http-4",
              "observations": [o.model_dump() for o in build_fixture("staggered_events")],
              "control_group": "never_treated", "min_event_time": -2, "max_event_time": 1}
        r = c.post("/api/v1/event-study", json=ok)
        assert r.status_code == 200, r.text
        points = {p["event_time"]: p["estimate"] for p in r.json()["points"]}
        assert abs(points[1] - 8.0) < 1e-9

        bad = {"request_id": "http-5",
               "observations": [o.model_dump() for o in build_fixture("staggered_events")],
               "control_group": "never_treated", "min_event_time": -2, "max_event_time": 2}
        r = c.post("/api/v1/event-study", json=bad)
        assert r.status_code == 422
        assert r.json()["failure_category"] == "OUT_OF_SUPPORT_EVENT_TIME"
