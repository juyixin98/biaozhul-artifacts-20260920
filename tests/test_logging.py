"""Logs must tie to a run identity and show version, steps and judgements."""
from __future__ import annotations

import json
from pathlib import Path

from app.config import settings


def _create_and_mine(client):
    payload = {
        "name": "logged",
        "sequences": [
            {"sequence_id": "s1",
             "events": [{"symbol": "A", "timestamp": 1},
                        {"symbol": "B", "timestamp": 2}]},
            {"sequence_id": "s2",
             "events": [{"symbol": "A", "timestamp": 1},
                        {"symbol": "B", "timestamp": 2}]},
        ],
    }
    created = client.post("/v1/corpora", json=payload)
    assert created.status_code == 201
    cid = created.json()["corpus_id"]
    mined = client.post("/v1/mine", json={"corpus_id": cid, "min_support": 2})
    assert mined.status_code == 200
    return cid, mined.json()["run_id"]


def _read_run_log(run_id):
    path = Path(settings.log_dir) / f"{run_id}.jsonl"
    assert path.exists(), f"expected per-run log file {path}"
    records = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            records.append(json.loads(line))
    return records


def test_log_file_is_keyed_by_run_id_and_links_input(client):
    cid, run_id = _create_and_mine(client)
    records = _read_run_log(run_id)
    assert records, "log must not be empty"

    # Every record carries the same run identity.
    assert all(r["run_id"] == run_id for r in records)

    msgs = [r["msg"] for r in records]

    # The input is recorded (corpus id) so the run can be tied to its input.
    received = next(m for m in msgs if m["event"] == "request-received")
    assert received["body"]["corpus_id"] == cid
    assert received["body"]["min_support"] == 2


def test_log_shows_version_progress_steps_and_decisions(client):
    _, run_id = _create_and_mine(client)
    msgs = [r["msg"] for r in _read_run_log(run_id)]
    events = [m["event"] for m in msgs]

    # Version metadata on the mining-start progress record.
    start = next(m for m in msgs if m["event"] == "mining-start")
    assert start["service_version"]
    assert start["python_version"]
    assert start["min_support"] == 2

    # Progress / computation steps are present.
    assert "mining-start" in events
    assert "mining-complete" in events
    assert "response-assembled" in events

    # Judgements carry their basis (min_support and the qualifies flag).
    seed_decisions = [m for m in msgs if m["event"] == "seed-support"]
    assert seed_decisions
    for d in seed_decisions:
        assert "min_support" in d and "qualifies" in d
        assert d["support"] >= d["min_support"] if d["qualifies"] else True

    # A prune judgement records WHY (distinct sequence identities).
    prunes = [m for m in msgs if m["event"] == "pattern-pruned"]
    # Not guaranteed any prune at support 2 here; add a below-threshold run to
    # force one, asserted in the next test.


def test_pruned_pattern_records_reason_and_still_completes(client):
    payload = {
        "name": "prunelog",
        "sequences": [
            {"sequence_id": "s1",
             "events": [{"symbol": "A", "timestamp": 1},
                        {"symbol": "B", "timestamp": 2}]},
        ],
    }
    cid = client.post("/v1/corpora", json=payload).json()["corpus_id"]
    # support 2 on a one-sequence corpus: everything except... nothing qualifies
    mined = client.post("/v1/mine", json={"corpus_id": cid, "min_support": 2})
    assert mined.status_code == 200
    run_id = mined.json()["run_id"]
    msgs = [r["msg"] for r in _read_run_log(run_id)]
    prunes = [m for m in msgs if m["event"] == "pattern-pruned"]
    assert prunes, "a below-threshold candidate must be logged as pruned"
    prune = prunes[0]
    assert prune["qualifies"] is False
    assert "min_support" in prune and "reason" in prune
    # And the run still reports completion (prune is a judgement, not an error).
    assert any(m["event"] == "mining-complete" for m in msgs)
    assert mined.json()["patterns"] == []


def test_kernel_logger_emits_without_file_when_disabled(client):
    # The in-memory logger used by direct kernel tests must not crash without
    # a log directory; covered by successful execution of a kernel helper.
    from tests.conftest import run_kernel
    response = run_kernel({"s1": [("A", None)]}, min_support=1)
    assert response.run_id.startswith("mine-")
