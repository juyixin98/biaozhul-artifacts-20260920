"""End-to-end API tests: auth, concealment, envelopes, HTTP concurrency."""
from __future__ import annotations

import concurrent.futures

import pytest
from fastapi.testclient import TestClient

from stratblock.api import create_app

STUDY_BODY = {
    "study_id": "trial-X",
    "arms": ["control", "treatment"],
    "stratification_factors": ["site"],
    "block_sizes": [2, 4],
    "allocation_ratio": [1, 1],
    "tail_policy": "permuted",
}

AUTH = {
    "enroller": {"Authorization": "Bearer enrol-token"},
    "auditor": {"Authorization": "Bearer audit-token"},
    "admin": {"Authorization": "Bearer admin-token"},
}


@pytest.fixture()
def client(api_config):
    app = create_app(api_config)
    with TestClient(app) as c:
        yield c


def _register(client) -> None:
    r = client.post("/v1/studies", json=STUDY_BODY, headers=AUTH["admin"])
    assert r.status_code == 200, r.text


def _enroll(client, subject: str, site: str, request_id: str, role="enroller"):
    return client.post(
        f"/v1/studies/{STUDY_BODY['study_id']}/enroll",
        json={"subject_id": subject, "features": {"site": site},
              "request_id": request_id},
        headers=AUTH[role],
    )


@pytest.mark.integration
def test_health_and_version(client) -> None:
    assert client.get("/healthz").json()["status"] == "ok"
    v = client.get("/version").json()
    assert v["version"] == "1.0.0"
    assert v["stream_scheme"] == "stratblock-deterministic-rng-v1"


@pytest.mark.integration
def test_envelope_shape_and_request_id_echo(client) -> None:
    _register(client)
    r = _enroll(client, "S1", "A", "req-1")
    body = r.json()
    assert body["success"] is True
    assert body["error"] is None
    assert body["meta"]["service_version"] == "1.0.0"
    assert body["meta"]["endpoint"].endswith("/enroll")
    assert r.headers["X-Request-ID"]


@pytest.mark.integration
def test_authn_and_role_isolation(client) -> None:
    # No token.
    r = client.post("/v1/studies", json=STUDY_BODY)
    assert r.status_code == 401
    assert r.json()["error"]["category"] == "UNAUTHENTICATED"
    # Bad token.
    r = client.post("/v1/studies", json=STUDY_BODY,
                    headers={"Authorization": "Bearer nope"})
    assert r.status_code == 401
    # Enroller cannot register studies.
    r = client.post("/v1/studies", json=STUDY_BODY, headers=AUTH["enroller"])
    assert r.status_code == 403
    assert r.json()["error"]["category"] == "FORBIDDEN"


@pytest.mark.integration
def test_enroller_cannot_read_audit_or_seed(client) -> None:
    _register(client)
    _enroll(client, "S1", "A", "req-1")
    r = client.get("/v1/studies/trial-X/audit", headers=AUTH["enroller"])
    assert r.status_code == 403
    r = client.get("/v1/studies/trial-X/diagnostics", headers=AUTH["enroller"])
    assert r.status_code == 403
    # Auditor sees diagnostics but never the master seed.
    r = client.get("/v1/studies/trial-X/diagnostics", headers=AUTH["auditor"])
    assert r.status_code == 200
    provenance = r.json()["data"]["stream_provenance"]
    assert provenance["master_seed"] == "redacted (administrator role only)"
    # Admin does see it.
    r = client.get("/v1/studies/trial-X/diagnostics", headers=AUTH["admin"])
    assert r.json()["data"]["stream_provenance"]["master_seed"] == 20260927


@pytest.mark.integration
def test_allocation_concealment_from_enroller(client) -> None:
    _register(client)
    r = _enroll(client, "S1", "A", "req-1")
    data = r.json()["data"]
    assert set(data) == {
        "study_id", "subject_id", "request_id", "arm", "replayed",
        "allocated_at", "concealment",
    }
    assert "block" not in data and "seed" not in data and "position" not in data


@pytest.mark.integration
def test_repeat_and_feature_conflict_over_http(client) -> None:
    _register(client)
    first = _enroll(client, "S1", "A", "req-1").json()["data"]["arm"]
    again = _enroll(client, "S1", "A", "req-2").json()["data"]
    assert again["arm"] == first and again["replayed"] is True

    r = _enroll(client, "S1", "B", "req-3")
    assert r.status_code == 409
    err = r.json()["error"]
    assert err["category"] == "DUPLICATE_CONFLICT"
    assert err["details"]["original_arm"] == first


@pytest.mark.integration
def test_validation_errors_are_itemised(client) -> None:
    bad = dict(STUDY_BODY, block_sizes=[3])  # 3 cannot split 1:1
    r = client.post("/v1/studies", json=bad, headers=AUTH["admin"])
    assert r.status_code == 422
    problems = r.json()["error"]["details"]["problems"]
    assert any("block size 3" in p for p in problems)


@pytest.mark.integration
def test_unknown_study_and_subject_categories(client) -> None:
    r = _enroll(client, "S1", "A", "req-1")
    assert r.status_code == 404
    assert r.json()["error"]["category"] == "UNKNOWN_STUDY"


@pytest.mark.integration
def test_http_concurrent_enrollment_is_balanced_and_durable(client) -> None:
    # Fixed block size 4 and 32 subjects guarantee every block completes,
    # so the pooled count must be exactly 16/16 (no open-tail disclosure
    # needed to explain the result).
    body = dict(STUDY_BODY, study_id="trial-block4", block_sizes=[4])
    r = client.post("/v1/studies", json=body, headers=AUTH["admin"])
    assert r.status_code == 200

    def call(i: int):
        return client.post(
            "/v1/studies/trial-block4/enroll",
            json={"subject_id": f"H{i}", "features": {"site": "A"},
                  "request_id": f"h-req-{i}"},
            headers=AUTH["enroller"],
        )

    with concurrent.futures.ThreadPoolExecutor(max_workers=16) as pool:
        responses = list(pool.map(call, range(32)))

    assert all(r.status_code == 200 for r in responses)
    diag = client.get(
        "/v1/studies/trial-block4/diagnostics", headers=AUTH["auditor"]
    ).json()["data"]
    assert diag["conclusion"] == "PASS"
    assert diag["n_subjects"] == 32
    assert diag["audit_summary"]["enrolled"] == 32
    assert diag["failures"] == [] and diag["uncertainties"] == []
    pooled = diag["distribution_checks"]["pooled_arm_counts"]
    assert pooled == {"control": 16, "treatment": 16}
    for stratum in diag["strata"]:
        assert stratum["sequence_replay"]["reference_oracle_agrees"] is True


@pytest.mark.integration
def test_seal_flow_blocks_enrollment_and_reports_tail(client) -> None:
    # Size-4 study; stratum site "1" realises a same-arm first pair
    # (verified independently for study "trial-seal"), giving an
    # infeasible (2,0) prefix to flag.
    body = dict(STUDY_BODY, study_id="trial-seal", block_sizes=[4])
    r = client.post("/v1/studies", json=body, headers=AUTH["admin"])
    assert r.status_code == 200

    def seal_enroll(subject, site, req):
        return client.post(
            "/v1/studies/trial-seal/enroll",
            json={"subject_id": subject, "features": {"site": site},
                  "request_id": req},
            headers=AUTH["enroller"],
        )

    seal_enroll("S1", "1", "req-1")
    seal_enroll("S2", "1", "req-2")
    r = client.post("/v1/studies/trial-seal/seal", headers=AUTH["admin"])
    assert r.status_code == 200
    r = seal_enroll("S3", "1", "req-3")
    assert r.status_code == 409
    assert r.json()["error"]["category"] == "STRATUM_CLOSED"
    # Repeat still resolves.
    r = seal_enroll("S1", "1", "req-1-repeat")
    assert r.status_code == 200 and r.json()["data"]["replayed"] is True
    diag = client.get(
        "/v1/studies/trial-seal/diagnostics", headers=AUTH["auditor"]
    ).json()["data"]
    assert diag["study_status"] == "sealed"
    assert diag["uncertainties"][0]["kind"] == "DISCLOSED_TAIL_IMBALANCE"


@pytest.mark.integration
def test_effect_endpoint_is_scoped_and_outcomes_admin_only(client) -> None:
    _register(client)
    # Enroll a handful and feed synthetic outcomes.
    for i in range(8):
        _enroll(client, f"O{i}", "A", f"o-req-{i}")
    arms = {}
    for s in client.get("/v1/studies/trial-X/audit", headers=AUTH["admin"]).json()["data"]["events"]:
        if s["event_type"] == "SUBJECT_ENROLLED":
            arms[s["subject_id"]] = s["payload"]["arm"]
    for subject, arm in arms.items():
        y = 2.0 if arm == "treatment" else 0.0
        r = client.post(
            "/v1/studies/trial-X/outcomes",
            json={"subject_id": subject, "y": y},
            headers=AUTH["admin"],
        )
        assert r.status_code == 200
    # Auditor cannot write outcomes.
    r = client.post(
        "/v1/studies/trial-X/outcomes",
        json={"subject_id": "O0", "y": 9.0}, headers=AUTH["auditor"],
    )
    assert r.status_code == 403
    report = client.get(
        "/v1/studies/trial-X/effect", headers=AUTH["auditor"]
    ).json()["data"]
    assert report["proves_allocation_correct"] is False
    # Default ordering is arms[0] - arms[1] = control - treatment = -2.
    assert abs(report["difference_in_means"] - (-2.0)) < 1e-9
    reversed_report = client.get(
        "/v1/studies/trial-X/effect?arm_a=treatment&arm_b=control",
        headers=AUTH["auditor"],
    ).json()["data"]
    assert abs(reversed_report["difference_in_means"] - 2.0) < 1e-9


@pytest.mark.integration
def test_multi_arm_effect_requires_explicit_arms(client) -> None:
    body = {
        "study_id": "multi",
        "arms": ["C", "T1", "T2"],
        "stratification_factors": ["site"],
        "block_sizes": [3, 6],
        "allocation_ratio": [1, 1, 1],
    }
    assert client.post("/v1/studies", json=body, headers=AUTH["admin"]).status_code == 200
    for i in range(6):
        r = client.post(
            "/v1/studies/multi/enroll",
            json={"subject_id": f"M{i}", "features": {"site": "A"},
                  "request_id": f"m-{i}"},
            headers=AUTH["enroller"],
        )
        assert r.status_code == 200
        arm = r.json()["data"]["arm"]
        client.post(
            "/v1/studies/multi/outcomes",
            json={"subject_id": f"M{i}", "y": 1.0 if arm == "T1" else 0.0},
            headers=AUTH["admin"],
        )
    # Without arm params a 3-arm study is indeterminate (422).
    r = client.get("/v1/studies/multi/effect", headers=AUTH["auditor"])
    assert r.status_code == 422
    assert r.json()["error"]["category"] == "VALIDATION_ERROR"
    # Explicit arms work.
    r = client.get(
        "/v1/studies/multi/effect?arm_a=T1&arm_b=C", headers=AUTH["auditor"]
    )
    assert r.status_code == 200
    assert r.json()["data"]["comparison"] == {"arm_a": "T1", "arm_b": "C"}


@pytest.mark.integration
def test_outcome_for_unknown_subject_is_404_and_replay_seed_redaction(client) -> None:
    _register(client)
    r = client.post(
        "/v1/studies/trial-X/outcomes",
        json={"subject_id": "ghost", "y": 1.0}, headers=AUTH["admin"],
    )
    assert r.status_code == 404
    assert r.json()["error"]["category"] == "UNKNOWN_SUBJECT"
    _enroll(client, "S1", "A", "req-1")
    auditor = client.post("/v1/studies/trial-X/replay", headers=AUTH["auditor"]).json()["data"]
    assert "master_seed" not in auditor
    admin = client.post("/v1/studies/trial-X/replay", headers=AUTH["admin"]).json()["data"]
    assert admin["master_seed"] == 20260927
