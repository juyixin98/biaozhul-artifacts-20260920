"""End-to-end HTTP tests (routing, error envelope, request correlation)."""
import pytest
from fastapi.testclient import TestClient

from app.api.app import create_app
from app.config import Settings

FIXTURE_SQL = """
CREATE TABLE R (id TEXT, b INTEGER, __row_id TEXT, __weight REAL);
INSERT INTO R VALUES ('a', 1, 'r1', 2.0), ('a', 2, 'r2', 3.0),
                     ('b', 1, 'r3', 4.0);
CREATE TABLE S (b INTEGER, c TEXT, __row_id TEXT, __weight REAL);
INSERT INTO S VALUES (1, 'x', 's1', 5.0), (NULL, 'z', 's3', 11.0);
"""

JOIN_QUERY = {
    "op": "join",
    "left": {"op": "relation", "relation": "R", "alias": "l"},
    "right": {"op": "relation", "relation": "S", "alias": "r"},
    "on": [["l.b", "r.b"]],
}


@pytest.fixture
def client(tmp_path):
    settings = Settings(
        db_path=str(tmp_path / "ev.db"),
        fixture_dir=str(tmp_path / "fixtures"),
        fixture_glob="*.sql",
        log_level="INFO",
        max_query_nodes=200,
        max_witnesses_per_answer=10000,
    )
    return TestClient(create_app(settings))


def _load(client):
    resp = client.post("/admin/load-sql", json={"sql": FIXTURE_SQL, "label": "e2e"})
    assert resp.status_code == 200, resp.text
    return resp.json()["version_id"]


def test_health(client):
    assert client.get("/health").json() == {"status": "ok"}


def test_query_join_and_verify_round_trip(client):
    version_id = _load(client)
    resp = client.post("/query", json={"query": JOIN_QUERY, "version_id": version_id})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["version_id"] == version_id
    by_values = {tuple(r["values"]): r for r in body["rows"]}
    # NULL-key s3 joins with nothing -> only key-1 binding present
    assert set(by_values) == {("a", 1, 1, "x"), ("b", 1, 1, "x")}
    row = by_values[("a", 1, 1, "x")]
    assert row["provenance"] == "R.r1*S.s1"

    # numeric verification through the API
    verify_resp = client.post(
        "/verify",
        json={"version_id": version_id, "rows": [row]},
    )
    assert verify_resp.status_code == 200, verify_resp.text
    verified = verify_resp.json()["verified"][0]
    assert verified["numeric_value"] == 10.0  # 2 * 5
    assert verified["matches"] is True

    # independently-supplied expectation: correct and incorrect values
    ok = client.post("/verify", json={"version_id": version_id, "rows": [
        {**row, "expected_value": 10.0}
    ]}).json()["verified"][0]
    assert ok["matches"] is True

    bad = client.post("/verify", json={"version_id": version_id, "rows": [
        {**row, "expected_value": 999.0}
    ]}).json()["verified"][0]
    assert bad["matches"] is False
    assert bad["numeric_value"] == 10.0


def test_request_id_is_generated_and_echoed(client):
    resp = client.get("/health")
    generated = resp.headers["x-request-id"]
    assert len(generated) == 32

    resp = client.get("/health", headers={"x-request-id": "trace-abc"})
    assert resp.headers["x-request-id"] == "trace-abc"


def test_unknown_version_returns_named_category(client):
    _load(client)
    resp = client.post("/query", json={"query": JOIN_QUERY, "version_id": 999})
    assert resp.status_code == 404
    err = resp.json()
    assert err["category"] == "UNKNOWN_VERSION"
    assert err["request_id"] == resp.headers["x-request-id"]


def test_unknown_column_returns_named_category(client):
    version_id = _load(client)
    bad_query = {
        "op": "select",
        "condition": {"col": "R.ghost", "op": "=", "value": 1},
        "input": {"op": "relation", "relation": "R"},
    }
    resp = client.post("/query", json={"query": bad_query, "version_id": version_id})
    assert resp.status_code == 400
    assert resp.json()["category"] == "UNKNOWN_COLUMN"


def test_null_comparison_literal_rejected(client):
    version_id = _load(client)
    bad_query = {
        "op": "select",
        "condition": {"col": "R.b", "op": "=", "value": None},
        "input": {"op": "relation", "relation": "R"},
    }
    resp = client.post("/query", json={"query": bad_query, "version_id": version_id})
    assert resp.status_code == 400
    assert resp.json()["category"] == "NULL_PREDICATE_NOT_SUPPORTED"


def test_malformed_request_body_is_422_with_request_id(client):
    resp = client.post("/query", json={"version_id": 1})
    assert resp.status_code == 422
    assert resp.json()["category"] == "MALFORMED_REQUEST"
    assert resp.json()["request_id"]


def test_versions_endpoint_lists_loaded_data(client):
    version_id = _load(client)
    resp = client.get("/versions")
    labels = {v["version_id"]: v["label"] for v in resp.json()["versions"]}
    assert labels[version_id] == "e2e"
    schema = client.get(f"/versions/{version_id}/schema").json()
    assert set(schema["schema"]) == {"R", "S"}
