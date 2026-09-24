"""End-to-end HTTP tests (FastAPI TestClient) and cryptographic integrity tests."""

import hashlib
import json

from fastapi.testclient import TestClient

from app.main import app
from app.analyzer import canonical_request_json
from app.models import AnalysisRequest

client = TestClient(app)


SCHED = {
    "tasks": [
        {"id": "T1", "wcet": 1, "period": 4, "deadline": 3, "blocking": 0, "priority": 1},
        {"id": "T2", "wcet": 2, "period": 8, "deadline": 8, "blocking": 1, "priority": 2},
        {"id": "T3", "wcet": 3, "period": 12, "deadline": 12, "blocking": 0, "priority": 3},
    ]
}


class TestEndpoints:
    def test_health(self):
        r = client.get("/health")
        assert r.status_code == 200
        assert r.json()["status"] == "ok"

    def test_root_lists_endpoints(self):
        r = client.get("/")
        assert r.status_code == 200
        assert "/api/v1/analyze" in r.json()["endpoints"]

    def test_analyze_schedulable(self):
        r = client.post("/api/v1/analyze", json=SCHED)
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["schedulable"] is True
        ids = [t["id"] for t in body["tasks"]]
        assert ids == ["T1", "T2", "T3"]
        # Each task reports interference sources and full iterations.
        t3 = next(t for t in body["tasks"] if t["id"] == "T3")
        srcs = {s["task_id"] for s in t3["interference_sources"]}
        assert srcs == {"T1", "T2"}
        assert len(t3["iterations"]) >= 2
        assert t3["failed_deadline"] is None
        assert t3["slack"] == 5
        assert body["simulation"]["crosscheck"] == "agree"

    def test_analyze_unschedulable_reports_failed_deadline(self):
        payload = {
            "tasks": [
                {"id": "T1", "wcet": 1, "period": 3, "deadline": 3, "priority": 1},
                {"id": "T2", "wcet": 2, "period": 7, "deadline": 2, "priority": 2},
            ]
        }
        r = client.post("/api/v1/analyze", json=payload)
        assert r.status_code == 200
        body = r.json()
        assert body["schedulable"] is False
        t2 = next(t for t in body["tasks"] if t["id"] == "T2")
        assert t2["terminal_condition"] == "deadline_exceeded"
        assert t2["failed_deadline"] == 2
        assert body["simulation"]["deadline_misses"] >= 1

    def test_reject_deadline_gt_period_http(self):
        bad = {"tasks": [{"id": "T1", "wcet": 1, "period": 4, "deadline": 9, "priority": 1}]}
        r = client.post("/api/v1/analyze", json=bad)
        assert r.status_code == 422
        err = r.json()
        assert err["error"] == "model_violation"
        assert err["code"] == "invalid_input"

    def test_reject_equal_priorities_http(self):
        bad = {"tasks": [
            {"id": "A", "wcet": 1, "period": 4, "deadline": 4, "priority": 1},
            {"id": "B", "wcet": 1, "period": 5, "deadline": 5, "priority": 1},
        ]}
        r = client.post("/api/v1/analyze", json=bad)
        assert r.status_code == 422
        assert "equal priorities" in r.json()["detail"]

    def test_reject_missing_field_http(self):
        r = client.post("/api/v1/analyze", json={"tasks": [{"id": "X", "wcet": 1}]})
        assert r.status_code == 422
        assert r.json()["error"] == "model_violation"

    def test_reject_non_json_http(self):
        r = client.post("/api/v1/analyze", content=b"not json{", headers={"content-type": "application/json"})
        assert r.status_code == 400
        assert r.json()["code"] == "invalid_json"

    def test_reject_json_array_http(self):
        r = client.post("/api/v1/analyze", json=[1, 2, 3])
        assert r.status_code == 422

    def test_low_utilization_is_not_sufficient(self):
        # The service must call this unschedulable even though U is only 0.619.
        payload = {
            "tasks": [
                {"id": "T1", "wcet": 1, "period": 3, "deadline": 3, "priority": 1},
                {"id": "T2", "wcet": 2, "period": 7, "deadline": 2, "priority": 2},
            ]
        }
        body = client.post("/api/v1/analyze", json=payload).json()
        assert body["utilization"]["total_u"] < 0.7
        assert body["schedulable"] is False
        assert "NOT a sufficient" in body["utilization"]["interpretation"]

    def test_iteration_overflow_never_success_http(self):
        bad_guard = {
            "tasks": [
                {"id": "T1", "wcet": 1, "period": 4, "deadline": 4, "priority": 1},
                {"id": "T2", "wcet": 2, "period": 7, "deadline": 7, "priority": 2},
            ],
            "options": {"max_iterations": 1},
        }
        body = client.post("/api/v1/analyze", json=bad_guard).json()
        assert body["schedulable"] is False
        t2 = next(t for t in body["tasks"] if t["id"] == "T2")
        assert t2["terminal_condition"] == "iteration_limit"

    def test_options_disable_simulation(self):
        body = client.post("/api/v1/analyze", json={**SCHED, "options": {"run_simulation": False}}).json()
        assert body["simulation"]["crosscheck"] == "skipped"


class TestIntegrity:
    def test_sha256_is_real_and_reproducible(self):
        b1 = client.post("/api/v1/analyze", json=SCHED).json()
        b2 = client.post("/api/v1/analyze", json=SCHED).json()
        assert b1["integrity"]["alg"] == "SHA-256"
        assert b1["integrity"]["sha256"] == b2["integrity"]["sha256"]
        # Independently recompute from the Pydantic-normalized request model.
        req = AnalysisRequest.model_validate(SCHED)
        expected = hashlib.sha256(canonical_request_json(req)).hexdigest()
        assert b1["integrity"]["sha256"] == expected
        assert len(expected) == 64

    def test_digest_verifiable_from_echoed_canonical_payload(self):
        body = client.post("/api/v1/analyze", json=SCHED).json()
        integ = body["integrity"]
        recomputed = hashlib.sha256(
            json.dumps(
                integ["canonical_payload"], sort_keys=True, separators=(",", ":")
            ).encode()
        ).hexdigest()
        assert recomputed == integ["sha256"]
        # The normalized payload exposes the default-expanded options.
        assert integ["canonical_payload"]["options"]["run_simulation"] is True

    def test_digest_changes_with_payload(self):
        a = client.post("/api/v1/analyze", json=SCHED).json()
        modified = json.loads(json.dumps(SCHED))
        modified["tasks"][0]["wcet"] = 2
        b = client.post("/api/v1/analyze", json=modified).json()
        assert a["integrity"]["sha256"] != b["integrity"]["sha256"]

    def test_key_order_does_not_change_digest(self):
        # JSON object key order is irrelevant after canonicalization (keys are
        # sorted); the array order is preserved as it is semantic.
        same_order = {
            "tasks": [
                {"id": "T1", "priority": 1, "blocking": 0, "deadline": 3, "period": 4, "wcet": 1},
                {"id": "T2", "priority": 2, "blocking": 1, "deadline": 8, "period": 8, "wcet": 2},
                {"id": "T3", "priority": 3, "blocking": 0, "deadline": 12, "period": 12, "wcet": 3},
            ]
        }
        h1 = client.post("/api/v1/analyze", json=SCHED).json()["integrity"]["sha256"]
        h2 = client.post("/api/v1/analyze", json=same_order).json()["integrity"]["sha256"]
        assert h1 == h2

    def test_digest_endpoint(self):
        r = client.post("/api/v1/digest", json={"b": 1, "a": 2})
        canonical = json.dumps({"a": 2, "b": 1}, sort_keys=True, separators=(",", ":")).encode()
        assert r.json()["sha256"] == hashlib.sha256(canonical).hexdigest()
