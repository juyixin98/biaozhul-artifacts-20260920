"""End-to-end HTTP API tests (real server app, real Z3 runs)."""

from fastapi.testclient import TestClient

from cbmc.api import app
from cbmc.fixtures import get_fixture

client = TestClient(app)


def test_healthz():
    assert client.get("/healthz").json() == {"status": "ok"}


def test_list_and_get_fixture():
    listing = client.get("/api/fixtures").json()["fixtures"]
    ids = {f["id"] for f in listing}
    assert "safe-transfer" in ids and "escrow-missing-debit" in ids

    doc = client.get("/api/fixtures/escrow-missing-debit").json()
    assert doc["name"] == "escrow-missing-debit"

    assert client.get("/api/fixtures/does-not-exist").status_code == 404


def test_check_endpoint_finds_and_replays_counterexample():
    resp = client.post("/api/check", json={
        "contract": get_fixture("escrow-missing-debit"),
        "steps": 5,
        "timeout_ms": 5000,
    })
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "counterexample"
    assert body["depth"] == 2
    # automatic replay attached and independently verified
    assert body["replay"]["executable"] is True
    assert body["replay"]["states_match_solver"] is True
    assert "conservation:0" in body["replay"]["violated"]


def test_check_endpoint_safe_contract_is_bounded_only():
    resp = client.post("/api/check", json={
        "contract": get_fixture("safe-transfer"),
        "steps": 12,
        "timeout_ms": 10_000,
    })
    body = resp.json()
    assert body["status"] == "safe_within_bound"
    assert "NOT a proof" in body["note"]


def test_fixture_check_shortcut():
    resp = client.post("/api/fixtures/uint8-pool-overflow/check?steps=3")
    body = resp.json()
    assert body["status"] == "counterexample"
    assert body["depth"] == 1


def test_replay_endpoint_rejects_disabled_action():
    bad_trace = [
        {"step": 0, "action": None, "params": {},
         "state": {"buyer": 10, "seller": 0, "escrow": 0, "locked": False}},
        {"step": 1, "action": "settle", "params": {},
         "state": {"buyer": 10, "seller": 10, "escrow": 10, "locked": False}},
    ]
    resp = client.post("/api/replay", json={
        "contract": get_fixture("escrow-missing-debit"),
        "trace": bad_trace,
    })
    assert resp.status_code == 422
    assert "disabled" in resp.json()["detail"]


def test_invalid_contract_returns_400():
    resp = client.post("/api/check", json={
        "contract": {"schema": "nope", "actions": []},
        "steps": 3,
    })
    assert resp.status_code == 400


def test_check_selection_of_property_subset():
    resp = client.post("/api/check", json={
        "contract": get_fixture("escrow-missing-debit"),
        "steps": 4,
        "checks": ["nonnegative"],
    })
    assert resp.json()["status"] == "safe_within_bound"
