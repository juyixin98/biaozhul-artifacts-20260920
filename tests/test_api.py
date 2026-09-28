"""End-to-end HTTP tests via FastAPI TestClient + SQLite evidence store."""

from __future__ import annotations

import json
import os

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def client(tmp_path, monkeypatch):
    db = tmp_path / "test.sqlite3"
    monkeypatch.setenv("MINIOWL_DB", str(db))
    # Import after env is set so settings pick up the temp DB.
    import importlib

    import min_iowl.config as config
    importlib.reload(config)
    from min_iowl.api import app as app_module
    importlib.reload(app_module)
    with TestClient(app_module.app) as c:
        c.db_path = str(db)
        yield c


FIXTURE = {
    "ontology_id": "test-onto",
    "axioms": [
        {"type": "SubClassOf",
         "sub": {"type": "Class", "name": "Dog"},
         "sup": {"type": "Class", "name": "Animal"}},
        {"type": "ClassAssertion",
         "individual": "rex",
         "class": {"type": "Class", "name": "Dog"}},
    ],
}


def test_health_reports_engine_version(client) -> None:
    res = client.get("/health")
    assert res.status_code == 200
    body = res.json()
    assert body["status"] == "ok"
    assert body["engine_version"].startswith("miniowl-kernel/")
    assert "request_id" in body


def test_request_id_is_correlated_across_header_body_and_log(client) -> None:
    rid = "corr-1234"
    res = client.post("/ontologies", json=FIXTURE, headers={"x-request-id": rid})
    assert res.status_code == 201
    assert res.headers["x-request-id"] == rid
    assert res.json()["request_id"] == rid
    # the same id is persisted in the structured request log with stages
    events = client.get(f"/requests/{rid}").json()["events"]
    stages = [e["stage"] for e in events]
    assert "received" in stages and "stored" in stages and "completed" in stages
    for ev in events:
        assert ev["path"]  # every log row pinpoints processing location


def test_create_and_reason_concrete_results(client) -> None:
    client.post("/ontologies", json=FIXTURE)
    res = client.post("/ontologies/test-onto/reason")
    assert res.status_code == 200
    body = res.json()
    # concrete derived result, not just "endpoint callable"
    rex = next(i for i in body["results"]["individuals"] if i["individual"] == "rex")
    assert rex["types"] == ["Animal", "Dog"]
    assert body["state"]["ontology_inconsistent"] is False
    assert body["state"]["unsatisfiable_classes"] == []
    # oracle cross-check present and agreeing
    assert body["cross_check"]["agrees"] is True
    assert body["cross_check"]["labels_explored"] == 4  # 2^2 classes
    assert body["engine_version"]
    # run persisted and retrievable
    run_id = body["run_id"]
    stored = client.get(f"/runs/{run_id}").json()
    assert stored["ontology_id"] == "test-onto"
    assert stored["cross_check"]["agrees"] is True


def test_proof_tree_explains_derivation_steps(client) -> None:
    client.post("/ontologies", json=FIXTURE)
    body = client.post("/ontologies/test-onto/reason").json()
    rex = next(i for i in body["results"]["individuals"] if i["individual"] == "rex")
    animal = rex["proofs"]["Animal"]
    # Animal is derived via a rule whose child is the asserted Dog fact
    assert animal["node"] == "derived"
    assert animal["children"][0]["node"] == "fact"
    assert animal["children"][0]["source_axiom"] == "a002"
    assert animal["source_axiom"] == "a001"


def test_unsupported_construct_rejected_with_category(client) -> None:
    bad = {
        "ontology_id": "bad-onto",
        "axioms": [
            {"type": "SubClassOf",
             "sub": {"type": "ObjectSomeValuesFrom",
                     "property": {"type": "ObjectProperty", "name": "hasPet"},
                     "filler": {"type": "Class", "name": "Cat"}},
             "sup": {"type": "Class", "name": "PetOwner"}}
        ],
    }
    res = client.post("/ontologies", json=bad)
    assert res.status_code == 422
    err = res.json()["error"]
    assert err["code"] == "UNSUPPORTED_CONSTRUCTOR"
    assert err["position"]  # rejected node pinpointed


def test_unsatisfiable_class_but_consistent_ontology_sections(client) -> None:
    data = json.load(open(os.path.join("data", "fixture_intersection_disjoint.json")))
    client.post("/ontologies", json={"ontology_id": "u", "axioms": data["axioms"]})
    body = client.post("/ontologies/u/reason").json()
    assert body["state"]["ontology_inconsistent"] is False
    assert body["state"]["unsatisfiable_classes"] == ["Hermaphrodite"]
    codes = {f["code"] for f in body["failures"]}
    assert "UNSATISFIABLE_CLASS" in codes
    assert "ONTOLOGY_INCONSISTENT" not in codes
    assert body["cross_check"]["agrees"] is True


def test_mutex_instance_conflict_has_path_and_marks_inconsistent(client) -> None:
    data = json.load(open(os.path.join("data", "fixture_mutex_instance.json")))
    client.post("/ontologies", json={"ontology_id": "m", "axioms": data["axioms"]})
    body = client.post("/ontologies/m/reason").json()
    assert body["state"]["ontology_inconsistent"] is True
    conflict = next(
        f for f in body["failures"] if f["code"] == "MUTEX_INSTANCE_CONFLICT"
    )
    assert conflict["individual"] == "mallory"
    detail = conflict["conflict"]
    assert detail["disjoint_pair"] == ["Male", "Female"]
    # the conflict path proves BOTH sides from declared assertions
    preds = {p["predicate"] for p in detail["conflict_path"]}
    assert preds == {"Male", "Female"}
    sources = set(detail["sources"])
    assert "a006" in sources and "a007" in sources
    # ontology-level failure is listed separately
    assert any(f["code"] == "ONTOLOGY_INCONSISTENT" for f in body["failures"])
    # uncertainty is called out explicitly under inconsistency
    assert any(u["code"] == "EX_FALSO_QUALIFICATION" for u in body["uncertain"])


def test_query_individual_endpoint_explains(client) -> None:
    client.post("/ontologies", json=FIXTURE)
    res = client.post(
        "/ontologies/test-onto/query", params={"individual": "rex"}
    )
    assert res.status_code == 200
    assert res.json()["entailed_types"] == ["Animal", "Dog"]
    assert "failure" not in res.json()


def test_query_unknown_individual_is_404_with_category(client) -> None:
    client.post("/ontologies", json=FIXTURE)
    res = client.post("/ontologies/test-onto/query", params={"individual": "ghost"})
    assert res.status_code == 404
    assert res.json()["error"]["code"] == "UNKNOWN_INDIVIDUAL"


def test_unknown_ontology_404(client) -> None:
    res = client.post("/ontologies/nope/reason")
    assert res.status_code == 404
    assert res.json()["error"]["code"] == "NOT_FOUND"


def test_duplicate_ontology_id_conflict(client) -> None:
    client.post("/ontologies", json=FIXTURE)
    res = client.post("/ontologies", json=FIXTURE)
    assert res.status_code == 409
    assert res.json()["error"]["code"] == "CONFLICT"


def test_functional_syntax_input(client) -> None:
    payload = {
        "ontology_id": "func-onto",
        "functional": "SubClassOf(A B)\nClassAssertion(A i1)",
    }
    res = client.post("/ontologies", json=payload)
    assert res.status_code == 201
    body = client.post("/ontologies/func-onto/reason").json()
    i1 = next(i for i in body["results"]["individuals"] if i["individual"] == "i1")
    assert i1["types"] == ["A", "B"]


def test_steps_report_processing_locations(client) -> None:
    client.post("/ontologies", json=FIXTURE)
    body = client.post("/ontologies/test-onto/reason").json()
    steps = {s["step"]: s for s in body["steps"]}
    for name in ["compile", "saturate", "equivalence_partition", "oracle_cross_check"]:
        assert steps[name]["status"] == "ok"
        assert steps[name]["detail"]
