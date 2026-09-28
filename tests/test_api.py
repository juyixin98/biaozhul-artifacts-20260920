"""HTTP boundary tests: categorical error envelopes and happy paths."""

from __future__ import annotations


def _chain_corpus() -> dict:
    return {
        "records": [
            {"id": "A", "name": "Pioneer Corp"},
            {"id": "B", "name": "Pioneer Corporation"},
            {"id": "C", "name": "Pioneer Foods"},
        ],
        "cannot_links": [["A", "C"]],
    }


def test_health(client) -> None:
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


def test_load_and_resolve_flow(client) -> None:
    resp = client.post("/corpus", json=_chain_corpus())
    assert resp.status_code == 201, resp.text

    resp = client.post("/resolve?threshold=0.45")
    assert resp.status_code == 200, resp.text
    data = resp.json()
    groups = {frozenset(c["members"]) for c in data["clusters"]}
    assert frozenset({"A", "B"}) in groups
    assert frozenset({"C"}) in groups
    assert data["run_id"].startswith("resolve-")


def test_input_error_envelope_on_bad_payload(client) -> None:
    resp = client.post("/corpus", json={"records": [{"id": "A"}]})  # missing name
    assert resp.status_code == 400
    body = resp.json()["error"]
    assert body["category"] == "INPUT_ERROR"
    assert body["code"].startswith("INPUT_ERROR")


def test_constraint_conflict_is_409_state_conflict(client) -> None:
    client.post(
        "/corpus",
        json={
            "records": [
                {"id": "A", "name": "X"},
                {"id": "B", "name": "Y"},
            ],
            "must_links": [["A", "B"]],
        },
    )
    resp = client.post("/links", json={"left": "A", "right": "B", "kind": "cannot"})
    assert resp.status_code == 409
    body = resp.json()["error"]
    assert body["category"] == "STATE_CONFLICT"
    assert body["details"]["reason"] == "direct_contradiction"


def test_record_not_found_is_404(client) -> None:
    client.post("/corpus", json=_chain_corpus())
    resp = client.post(
        "/links", json={"left": "A", "right": "GHOST", "kind": "must"}
    )
    assert resp.status_code == 404
    assert resp.json()["error"]["category"] == "INPUT_ERROR"


def test_bad_threshold_is_input_error(client) -> None:
    client.post("/corpus", json=_chain_corpus())
    resp = client.post("/resolve?threshold=1.5")
    assert resp.status_code == 400
    assert resp.json()["error"]["category"] == "INPUT_ERROR"


def test_resource_exhausted_is_507(exact_client) -> None:
    # 9 mutually dissimilar records -> Bell(9) > 100 partition budget.
    records = [{"id": f"n{i}", "name": f"Distinct Name {i}"} for i in range(9)]
    load = exact_client.post("/corpus", json={"records": records})
    assert load.status_code == 201, load.text
    resp = exact_client.post("/resolve")
    assert resp.status_code == 507
    body = resp.json()["error"]
    assert body["category"] == "RESOURCE_EXHAUSTED"
    assert body["details"]["blocks"] == 9


def test_computation_failure_is_distinct_category(client, monkeypatch) -> None:
    # Force an unexpected internal error and confirm it maps to 500, not to a
    # state/input category.
    def boom(*_args, **_kwargs):
        raise RuntimeError("synthetic internal failure")

    monkeypatch.setattr(client.app.state.service.storage, "count_records", boom)
    resp = client.get("/health")
    assert resp.status_code == 500
    assert resp.json()["error"]["category"] == "COMPUTATION_FAILED"


def test_lock_endpoint_and_affected(client) -> None:
    client.post("/corpus", json=_chain_corpus())
    client.post("/resolve?threshold=0.45")
    resp = client.post(
        "/clusters/lock",
        json={"cluster_id": "lock-1", "members": ["A", "B"]},
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["locked"] is True

    resp = client.get("/affected")
    assert resp.status_code == 200
    assert set(resp.json()["changed_records"]) >= {"A", "B"}


def test_lock_unknown_record_is_input_error(client) -> None:
    client.post("/corpus", json=_chain_corpus())
    resp = client.post(
        "/clusters/lock",
        json={"cluster_id": "lock-x", "members": ["A", "ZZZ"]},
    )
    assert resp.status_code == 400
    assert resp.json()["error"]["category"] == "INPUT_ERROR"
