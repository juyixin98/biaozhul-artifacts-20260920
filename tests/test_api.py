"""API tests: happy path, failure categories, run identity and log linkage."""

import logging

import pytest

from app.corpus.fixtures import load_fixture


def create_corpus(client, corpus) -> str:
    resp = client.post("/corpora", json=corpus)
    assert resp.status_code == 201, resp.text
    return resp.json()["corpus_id"]


def test_health_and_meta(client):
    assert client.get("/health").json() == {"status": "ok"}
    versions = client.get("/meta").json()["versions"]
    for key in ("app", "python", "fastapi", "pydantic", "sqlite"):
        assert key in versions and versions[key]


def test_mine_repeated_events_end_to_end(client, repeated_events_corpus):
    corpus_id = create_corpus(client, repeated_events_corpus)

    fetched = client.get(f"/corpora/{corpus_id}")
    assert fetched.status_code == 200
    assert fetched.json()["digest"]

    resp = client.post("/mine", json={"corpus_id": corpus_id, "min_support": 2})
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["status"] == "COMPLETED"
    actual = {">".join(p["pattern"]): p["support"] for p in body["patterns"]}
    assert actual == {"A": 3, "B": 3, "A>B": 2}

    evidence = next(p for p in body["patterns"] if p["pattern"] == ["A", "B"])
    assert {
        (e["sequence_id"], tuple(e["positions"])) for e in evidence["embeddings"]
    } == {("S1", (0, 2)), ("S1", (1, 2)), ("S2", (0, 1))}

    run = client.get(f"/runs/{body['run_id']}")
    assert run.status_code == 200
    run_body = run.json()
    assert run_body["status"] == "COMPLETED"
    assert run_body["pattern_count"] == 3
    versions = run_body["versions_json"]
    assert "python" in versions and "sqlite" in versions

    stored = client.get(f"/runs/{body['run_id']}/patterns").json()["patterns"]
    assert {">".join(p["pattern"]): p["support"] for p in stored} == actual


def test_mine_with_time_gap_on_timestamped_corpus(client):
    corpus_id = create_corpus(client, load_fixture("time_gap")["corpus"])
    resp = client.post(
        "/mine", json={"corpus_id": corpus_id, "min_support": 2, "max_time_gap": 4}
    )
    assert resp.status_code == 201
    actual = {">".join(p["pattern"]): p["support"] for p in resp.json()["patterns"]}
    assert actual == {"A": 3, "B": 3, "A>B": 2}


# ---- failure categories -------------------------------------------------


def test_unknown_corpus_mine_returns_404(client):
    resp = client.post("/mine", json={"corpus_id": "nope", "min_support": 1})
    assert resp.status_code == 404
    assert resp.json()["error"]["category"] == "not_found"


def test_unknown_run_returns_404(client):
    resp = client.get("/runs/does-not-exist")
    assert resp.status_code == 404
    assert resp.json()["error"]["category"] == "not_found"


@pytest.mark.parametrize(
    "params",
    [
        {"min_support": 0},
        {"min_support": 1, "max_pos_gap": 0},
        {"min_support": 1, "max_time_gap": -1},
        {"min_support": 1, "max_pattern_len": 0},
    ],
)
def test_invalid_mine_params_return_422(client, repeated_events_corpus, params):
    corpus_id = create_corpus(client, repeated_events_corpus)
    resp = client.post("/mine", json={"corpus_id": corpus_id, **params})
    assert resp.status_code == 422
    assert resp.json()["error"]["category"] == "validation_error"


def test_time_gap_without_timestamps_is_constraint_error_and_failed_run(
    client, repeated_events_corpus
):
    corpus_id = create_corpus(client, repeated_events_corpus)
    resp = client.post(
        "/mine", json={"corpus_id": corpus_id, "min_support": 1, "max_time_gap": 5}
    )
    assert resp.status_code == 422
    assert resp.json()["error"]["category"] == "constraint_error"
    # the run must be persisted as FAILED, never as success
    store = client.app.state.store
    row = store._conn.execute(
        "SELECT status, error FROM runs ORDER BY started_at DESC LIMIT 1"
    ).fetchone()
    assert row["status"] == "FAILED"
    assert "constraint_error" in row["error"]


@pytest.mark.parametrize(
    "corpus",
    [
        # duplicate sequence id
        {"name": "dup", "sequences": [
            {"sequence_id": "S1", "events": [{"symbol": "A"}]},
            {"sequence_id": "S1", "events": [{"symbol": "B"}]},
        ]},
        # decreasing timestamps
        {"name": "ts", "sequences": [
            {"sequence_id": "S1", "events": [
                {"symbol": "A", "timestamp": 5}, {"symbol": "B", "timestamp": 1}]},
        ]},
        # mixed timestamp presence
        {"name": "mixed", "sequences": [
            {"sequence_id": "S1", "events": [{"symbol": "A", "timestamp": 1}]},
            {"sequence_id": "S2", "events": [{"symbol": "B"}]},
        ]},
        # empty symbol
        {"name": "empty", "sequences": [
            {"sequence_id": "S1", "events": [{"symbol": ""}]},
        ]},
    ],
    ids=["duplicate_seq_id", "decreasing_ts", "mixed_ts", "empty_symbol"],
)
def test_invalid_corpus_rejected(client, corpus):
    resp = client.post("/corpora", json=corpus)
    assert resp.status_code == 422
    assert resp.json()["error"]["category"] == "validation_error"


# ---- log linkage ---------------------------------------------------------


class ListHandler(logging.Handler):
    def __init__(self):
        super().__init__()
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


def test_mining_logs_carry_run_identity_and_decisions(client, repeated_events_corpus):
    handler = ListHandler()
    logging.getLogger("fspm").addHandler(handler)
    try:
        corpus_id = create_corpus(client, repeated_events_corpus)
        resp = client.post("/mine", json={"corpus_id": corpus_id, "min_support": 2})
        run_id = resp.json()["run_id"]
    finally:
        logging.getLogger("fspm").removeHandler(handler)

    evals = [r for r in handler.records if getattr(r, "step", None) == "pattern_eval"]
    assert evals, "expected per-pattern evaluation log records"
    for record in evals:
        assert record.run_id == run_id
        assert record.decision in ("keep", "prune")
        assert record.support >= 0 and record.min_support == 2
    kept = {tuple(r.pattern) for r in evals if r.decision == "keep"}
    assert ("A", "B") in kept and ("A",) in kept
    pruned = {tuple(r.pattern) for r in evals if r.decision == "prune"}
    assert ("A", "A") in pruned  # support 1 < min_support 2
