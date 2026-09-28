"""End-to-end HTTP tests through the real FastAPI app + SQLite.

These assert concrete status codes and the explicit failure categories, not
just "the endpoint is callable".  Unknown/exceptional states must never be
reported as success.
"""
from __future__ import annotations


def _create_corpus(client, sequences, name="api-corpus"):
    payload = {
        "name": name,
        "sequences": [
            {"sequence_id": sid,
             "events": [{"symbol": s, "timestamp": t} for s, t in events]}
            for sid, events in sequences.items()
        ],
    }
    resp = client.post("/v1/corpora", json=payload)
    assert resp.status_code == 201, resp.text
    return resp.json()["corpus_id"]


def test_health_reports_ok_and_version(client):
    resp = client.get("/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert isinstance(body["version"], str) and body["version"]


def test_full_mine_flow_repeats_count_once(client):
    cid = _create_corpus(client, {
        "s1": [("A", 1), ("A", 2), ("B", 3)],
        "s2": [("A", 1), ("B", 2)],
    })
    resp = client.post("/v1/mine", json={
        "corpus_id": cid, "min_support": 2,
    })
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["corpus_id"] == cid
    assert body["corpus_size"] == 2
    assert body["constraints"]["min_support"] == 2
    assert body["run_id"].startswith("mine-")

    by_pattern = {tuple(p["pattern"]): p for p in body["patterns"]}
    ab = by_pattern[("A", "B")]
    assert ab["support"] == 2
    assert ab["supporting_sequence_ids"] == ["s1", "s2"]
    # s1 embeds twice ((0,2),(1,2)) yet contributes one identity.
    s1 = next(e for e in ab["evidence"] if e["sequence_id"] == "s1")
    assert {tuple(x["positions"]) for x in s1["embeddings"]} == {(0, 2), (1, 2)}


def test_mine_unknown_corpus_is_404_not_success(client):
    resp = client.post("/v1/mine", json={"corpus_id": "missing", "min_support": 1})
    assert resp.status_code == 404
    body = resp.json()
    assert body["error"] == "not_found"


def test_invalid_min_support_returns_constraint_failure(client):
    cid = _create_corpus(client, {"s1": [("A", None)]})
    resp = client.post("/v1/mine", json={"corpus_id": cid, "min_support": 0})
    assert resp.status_code == 422
    assert resp.json()["error"] == "invalid_constraint"


def test_fractional_support_out_of_range_is_rejected(client):
    cid = _create_corpus(client, {"s1": [("A", None)]})
    resp = client.post("/v1/mine", json={"corpus_id": cid, "min_support": 1.5})
    assert resp.status_code == 422
    assert resp.json()["error"] == "invalid_constraint"


def test_time_gap_without_timestamps_is_constraint_failure(client):
    cid = _create_corpus(client, {"s1": [("A", None), ("B", None)]})
    resp = client.post("/v1/mine", json={
        "corpus_id": cid, "min_support": 1, "max_gap_time": 5,
    })
    assert resp.status_code == 422
    assert resp.json()["error"] == "invalid_constraint"


def test_malformed_body_is_invalid_request_body_not_500(client):
    resp = client.post("/v1/mine", json={"corpus_id": "x"})  # missing min_support
    assert resp.status_code == 422
    assert resp.json()["error"] == "invalid_request_body"
    assert resp.json()["details"]["issues"]


def test_string_number_is_not_coerced(client):
    cid = _create_corpus(client, {"s1": [("A", None)]})
    resp = client.post("/v1/mine", json={"corpus_id": cid, "min_support": "1"})
    assert resp.status_code == 422
    assert resp.json()["error"] == "invalid_request_body"


def test_duplicate_corpus_creation_is_conflict(client):
    cid = _create_corpus(client, {"s1": [("A", None)]})
    # Same generated id cannot recur via API, but we can force persistence
    # conflict through the repository with a fixed id via direct service use.
    from app.corpus.spec import normalize_corpus
    from app.models import CorpusCreateRequest, EventIn, SequenceIn
    from app.storage.repository import CorpusRepository
    from app.errors import ConflictingCorpusError
    import pytest

    repo = CorpusRepository(db_path=client.db_path)
    request = CorpusCreateRequest(
        name="dup",
        sequences=[SequenceIn(sequence_id="s1", events=[EventIn(symbol="A")])],
    )
    corpus = normalize_corpus("fixed-id", request)
    repo.save_corpus(corpus, has_timestamps=False)
    with pytest.raises(ConflictingCorpusError) as exc:
        repo.save_corpus(corpus, has_timestamps=False)
    assert exc.value.code == "corpus_conflict"


def test_fixture_endpoints_round_trip(client):
    listing = client.get("/v1/fixtures")
    assert listing.status_code == 200
    assert "pruning_trap" in listing.json()["fixtures"]

    created = client.post("/v1/fixtures/time_ties")
    assert created.status_code == 201
    cid = created.json()["corpus_id"]

    mined = client.post("/v1/mine", json={
        "corpus_id": cid, "min_support": 1, "max_gap_time": 0,
    })
    assert mined.status_code == 200
    by_pattern = {tuple(p["pattern"]): p for p in mined.json()["patterns"]}
    assert set(by_pattern[("A", "B")]["supporting_sequence_ids"]) == {"s1", "s2"}

    unknown = client.post("/v1/fixtures/nope")
    assert unknown.status_code == 404
    assert unknown.json()["error"] == "not_found"


def test_delete_and_get_404(client):
    cid = _create_corpus(client, {"s1": [("A", None)]})
    assert client.delete(f"/v1/corpora/{cid}").status_code == 204
    assert client.get(f"/v1/corpora/{cid}").status_code == 404
    assert client.delete(f"/v1/corpora/{cid}").status_code == 404


def test_listing_and_persistence_across_requests(client):
    cid = _create_corpus(client, {"s1": [("A", None), ("B", None)]})
    listed = client.get("/v1/corpora").json()["corpora"]
    assert any(c["corpus_id"] == cid for c in listed)
    fetched = client.get(f"/v1/corpora/{cid}").json()
    assert fetched["sequences"][0]["events"][0]["position"] == 0
    assert fetched["sequences"][0]["events"][1]["position"] == 1


def test_unexpected_exception_is_500_not_success(client, monkeypatch):
    # Force an unexpected (non-domain) failure inside the mining service.
    def boom(*_args, **_kwargs):
        raise RuntimeError("simulated kernel catastrophe")

    monkeypatch.setattr(client.app.state.service, "mine", boom)
    resp = client.post("/v1/mine", json={"corpus_id": "x", "min_support": 1})
    assert resp.status_code == 500
    body = resp.json()
    assert body["error"] == "internal_error"
    assert body["details"]["run_id"] != "unbound"
    # The unexpected failure must be recorded in that run's log file.
    import json
    from pathlib import Path
    from app.config import settings
    log_path = Path(settings.log_dir) / f"{body['details']['run_id']}.jsonl"
    records = [json.loads(line) for line in log_path.read_text().splitlines() if line]
    assert any(r["msg"].get("event") == "unhandled-exception"
               and r["msg"].get("error_type") == "RuntimeError"
               for r in records)
