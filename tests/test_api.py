"""End-to-end API tests over an in-memory SQLite store."""

import pytest

pytestmark = pytest.mark.integration

from fastapi.testclient import TestClient

from conftest import load_fixture
from datalog_service.api.app import create_app
from datalog_service.storage.evidence_store import EvidenceStore


@pytest.fixture()
def client():
    store = EvidenceStore(":memory:")
    app = create_app(store)
    with TestClient(app) as test_client:
        yield test_client
    store.close()


@pytest.fixture()
def ancestor_program(client):
    response = client.post(
        "/programs",
        json={"program": load_fixture("ancestor.dl")},
        headers={"X-Request-ID": "submit-ancestor"},
    )
    assert response.status_code == 200
    return response.json()


def test_healthz(client):
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_submit_returns_versions_and_strata(client, ancestor_program):
    body = ancestor_program
    assert body["status"] == "ok"
    assert body["rule_version"]
    assert body["fact_set_version"]
    assert len(body["materialization_id"]) == 16
    assert body["strata"] == [["r1", "r2"]]
    assert body["rule_count"] == 2
    assert body["fact_count"] == 6


def test_request_id_header_is_echoed_and_logged(client, ancestor_program):
    record_response = client.get("/requests/submit-ancestor")
    assert record_response.status_code == 200
    record = record_response.json()["record"]
    assert record["request_id"] == "submit-ancestor"
    assert record["endpoint"] == "POST /programs"
    assert record["status"] == "ok"
    assert record["program_id"] == ancestor_program["program_id"]


def test_query_returns_concrete_bindings_and_proof(client, ancestor_program):
    program_id = ancestor_program["program_id"]
    response = client.post(
        f"/programs/{program_id}/query",
        json={"query": "ancestor(alice, Y)?", "include_proofs": True},
        headers={"X-Request-ID": "query-alice"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["answer_count"] == 6
    values = {a["bindings"]["Y"] for a in body["answers"]}
    assert values == {"bob", "frank", "carol", "grace", "dave", "erin"}
    # Every proof bottoms out in ground facts.
    for answer in body["answers"]:
        kinds = _collect_kinds(answer["proof"])
        assert "unexplained" not in kinds
        assert "depth_truncated" not in kinds
        assert "fact" in kinds
    # The longest chain (erin) shows a 4-fact path.
    erin = next(a for a in body["answers"] if a["bindings"]["Y"] == "erin")
    facts = [n for n in _flatten(erin["proof"]) if n["kind"] == "fact"]
    assert len(facts) == 4


def test_ground_query_returns_boolean_style_answer(client, ancestor_program):
    program_id = ancestor_program["program_id"]
    response = client.post(
        f"/programs/{program_id}/query",
        json={"query": "ancestor(alice, erin)?", "include_proofs": False},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["answer_count"] == 1
    assert body["answers"][0]["bindings"] == {}
    assert body["answers"][0]["proof"] is None

    negative = client.post(
        f"/programs/{program_id}/query",
        json={"query": "ancestor(erin, alice)?"},
    )
    assert negative.status_code == 200
    assert negative.json()["answer_count"] == 0


def test_unsafe_variable_is_a_422_with_stable_code(client):
    response = client.post(
        "/programs", json={"program": "q(a). p(X, Y) :- q(X)."}
    )
    assert response.status_code == 422
    body = response.json()
    assert body["error_code"] == "COMPILE_ERROR"
    codes = [issue["code"] for issue in body["failures"]]
    assert codes == ["UNSAFE_VARIABLE"]
    assert body["failures"][0]["location"]["variable"] == "Y"


def test_negation_cycle_is_a_422(client):
    program = "e(a). p(X) :- e(X), not q(X). q(X) :- e(X), not p(X)."
    response = client.post("/programs", json={"program": program})
    assert response.status_code == 422
    assert response.json()["error_code"] == "COMPILE_ERROR"
    assert response.json()["failures"][0]["code"] == "NEGATION_CYCLE"


def test_parse_error_is_a_400(client):
    response = client.post("/programs", json={"program": "parent(a b)."})
    assert response.status_code == 400
    assert response.json()["error_code"] == "PARSE_ERROR"


def test_unknown_program_id_is_a_404(client):
    response = client.post(
        "/programs/nope/query", json={"query": "p(X)?"}
    )
    assert response.status_code == 404
    assert response.json()["error_code"] == "STATE_ERROR"


def test_query_arity_mismatch_is_a_400(client, ancestor_program):
    response = client.post(
        f"/programs/{ancestor_program['program_id']}/query",
        json={"query": "ancestor(X)?"},
    )
    assert response.status_code == 400
    assert response.json()["error_code"] == "QUERY_ERROR"


def test_unknown_predicate_query_is_a_400(client, ancestor_program):
    response = client.post(
        f"/programs/{ancestor_program['program_id']}/query",
        json={"query": "ghost(X)?"},
    )
    assert response.status_code == 400
    assert response.json()["error_code"] == "QUERY_ERROR"


def test_tuples_endpoint_lists_derived_and_fact_rows(client, ancestor_program):
    program_id = ancestor_program["program_id"]
    mat_id = ancestor_program["materialization_id"]
    response = client.get(f"/programs/{program_id}/tuples/ancestor")
    assert response.status_code == 200
    body = response.json()
    assert body["count"] == 13
    kinds = {row["kind"] for row in body["tuples"]}
    # All ancestor tuples are derived (EDB facts live under "parent");
    # r1 copies parent pairs, r2 extends them.
    assert kinds == {"derived"}
    parent_rows = client.get(f"/programs/{program_id}/tuples/parent").json()
    assert {r["kind"] for r in parent_rows["tuples"]} == {"fact"}
    derived = body["tuples"]
    assert all(r["rule_id"] in {"r1", "r2"} for r in derived)
    assert all(r["round"] >= 0 for r in derived)


def test_duplicate_submit_is_content_deduped(client):
    source = load_fixture("ancestor.dl")
    first = client.post("/programs", json={"program": source}).json()
    second = client.post("/programs", json={"program": source}).json()
    assert first["program_id"] == second["program_id"]
    assert first["materialization_id"] == second["materialization_id"]
    assert second["reused"] is True


def test_stratified_negation_end_to_end(client):
    response = client.post(
        "/programs", json={"program": load_fixture("recursive_exclusion.dl")}
    )
    assert response.status_code == 200
    program_id = response.json()["program_id"]
    indirect = client.post(
        f"/programs/{program_id}/query",
        json={"query": "indirect(X, Y)?"},
    ).json()
    pairs = {(a["bindings"]["X"], a["bindings"]["Y"]) for a in indirect["answers"]}
    assert ("alice", "bob") not in pairs
    assert ("alice", "erin") in pairs
    assert indirect["answer_count"] == 7
    roots = client.post(
        f"/programs/{program_id}/query", json={"query": "root(X)?"}
    ).json()
    assert [a["bindings"]["X"] for a in roots["answers"]] == ["alice"]


def _collect_kinds(node):
    kinds = {node["kind"]}
    for child in node.get("children", []):
        kinds |= _collect_kinds(child)
    return kinds


def _flatten(node):
    nodes = [node]
    for child in node.get("children", []):
        nodes.extend(_flatten(child))
    return nodes


def test_describe_program_returns_normalized_rules(client, ancestor_program):
    program_id = ancestor_program["program_id"]
    response = client.get(f"/programs/{program_id}")
    assert response.status_code == 200
    body = response.json()
    assert body["rule_version"] == ancestor_program["rule_version"]
    assert {r["rule_id"] for r in body["rules"]} == {"r1", "r2"}
    assert "parent(alice, bob)" in body["normalized_text"]


def test_describe_unknown_program_is_404(client):
    assert client.get("/programs/missing").status_code == 404


def test_max_answers_truncation_flag(client, ancestor_program):
    program_id = ancestor_program["program_id"]
    response = client.post(
        f"/programs/{program_id}/query",
        json={"query": "ancestor(X, Y)?", "max_answers": 5},
    )
    body = response.json()
    assert body["truncated"] is True
    assert body["answer_count"] == 5


def test_invalid_max_answers_is_a_422(client, ancestor_program):
    response = client.post(
        f"/programs/{ancestor_program['program_id']}/query",
        json={"query": "p(X)?", "max_answers": 0},
    )
    assert response.status_code == 422


def test_unknown_request_id_is_404(client):
    assert client.get("/requests/nope").status_code == 404


def test_unknown_predicate_tuples_endpoint_is_400(client, ancestor_program):
    response = client.get(
        f"/programs/{ancestor_program['program_id']}/tuples/ghost"
    )
    assert response.status_code == 400
    assert response.json()["error_code"] == "QUERY_ERROR"


def test_rate_limit_returns_429_with_stable_code():
    from datalog_service.config import Settings

    limited_settings = Settings(
        db_path=":memory:", max_program_chars=200000, max_query_chars=2000,
        max_answers=1000, rate_limit_per_minute=2, service_name="test",
    )
    store = EvidenceStore(":memory:")
    app = create_app(store, settings=limited_settings)
    with TestClient(app) as test_client:
        first = test_client.get("/healthz")
        second = test_client.get("/healthz")
        third = test_client.get("/healthz")
    assert first.status_code == 200
    assert second.status_code == 200
    assert third.status_code == 429
    body = third.json()
    assert body["error_code"] == "RATE_LIMITED"
    assert "Retry-After" in third.headers
    store.close()
