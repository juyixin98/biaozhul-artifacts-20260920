"""Reliability tests: concurrent merge and crash-between-transactions recovery."""
from __future__ import annotations

import threading

import pytest

from conftest import add_snapshot, register_chain_and_validator, signed_vote


def test_concurrent_merge_punishes_exactly_once(client, validator):
    """Many threads deliver both conflicting votes (and repeats) in parallel."""
    register_chain_and_validator(client, "chain-a", validator)
    add_snapshot(client, "chain-a", validator, epoch=0, power=2_000_000)

    votes = [
        signed_vote(validator, chain_id="chain-a", height=77, round=3,
                    vote_type="prevote", block_hash=bh)
        for bh in ("11" * 32, "22" * 32)
    ]

    results: list[dict] = []
    errors: list[Exception] = []
    barrier = threading.Barrier(12)

    def worker(i):
        payload = votes[i % 2]
        try:
            barrier.wait()
            r = client.post("/votes", json=payload)
            assert r.status_code == 200, r.text
            results.append(r.json())
        except Exception as exc:  # pragma: no cover - surfaced as test failure
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(12)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == []
    statuses = [r["status"] for r in results]
    # The only invariant that matters under real concurrency: exactly one
    # request performs the slash, regardless of interleaving.
    assert statuses.count("penalized") == 1
    assert set(statuses) <= {
        "penalized", "already_penalized", "duplicate_vote", "accepted_no_conflict"
    }
    evidence_ids = {r["evidence_id"] for r in results if r.get("evidence_id")}
    assert len(evidence_ids) == 1

    penalties = client.get("/penalties").json()["penalties"]
    assert len(penalties) == 1
    assert penalties[0]["slashed_power"] == 20_000
    evidence = client.get("/evidence").json()["evidence"]
    assert len(evidence) == 1
    assert evidence[0]["status"] == "PENALIZED"


def test_crash_after_evidence_then_recovery_completes_penalty(client, validator, monkeypatch):
    """Simulate a process crash between evidence commit and penalty application."""
    register_chain_and_validator(client, "chain-a", validator)
    add_snapshot(client, "chain-a", validator, epoch=0, power=500_000)

    v1 = signed_vote(validator, chain_id="chain-a", height=6, round=0,
                     vote_type="prevote", block_hash="aa" * 32)
    v2 = signed_vote(validator, chain_id="chain-a", height=6, round=0,
                     vote_type="prevote", block_hash="bb" * 32)

    # --- crash phase: enable the injected fault ---
    monkeypatch.setattr("app.service.settings.crash_after_evidence", True)
    client.post("/votes", json=v1)
    with pytest.raises(RuntimeError, match="injected crash"):
        client.post("/votes", json=v2)

    # Evidence durably landed; penalty did not.
    evidence = client.get("/evidence").json()["evidence"]
    assert len(evidence) == 1
    eid = evidence[0]["evidence_id"]
    assert evidence[0]["status"] == "DETECTED"
    assert client.get("/penalties").json()["penalties"] == []

    # --- restart phase: crash flag cleared, recovery reconciles ---
    monkeypatch.setattr("app.service.settings.crash_after_evidence", False)
    report = client.post("/recover").json()
    assert report["recovered"] >= 1
    assert any(r["evidence_id"] == eid and r["status"] == "penalized"
               for r in report["results"])

    detail = client.get(f"/evidence/{eid}").json()
    assert detail["status"] == "PENALIZED"
    assert detail["penalty"]["slashed_power"] == 5_000

    # Recovery is idempotent: running it again changes nothing.
    again = client.post("/recover").json()
    assert again["recovered"] == 0
    assert len(client.get("/penalties").json()["penalties"]) == 1


def test_crash_with_missing_snapshot_recovers_after_snapshot(client, validator, monkeypatch):
    """Crash leaves DETECTED evidence; snapshot later supplied; startup recovery."""
    register_chain_and_validator(client, "chain-a", validator)
    v1 = signed_vote(validator, chain_id="chain-a", height=6, round=12,
                     vote_type="precommit", block_hash="cc" * 32)
    v2 = signed_vote(validator, chain_id="chain-a", height=6, round=12,
                     vote_type="precommit", block_hash="dd" * 32)
    client.post("/votes", json=v1)
    client.post("/votes", json=v2)  # -> awaiting_snapshot (no penalty possible)

    evidence = client.get("/evidence").json()["evidence"][0]
    assert evidence["status"] == "AWAITING_SNAPSHOT"

    # Simulate restart without a snapshot: recovery leaves it pending.
    report = client.post("/recover").json()
    assert any(r["status"] == "awaiting_snapshot" for r in report["results"])

    add_snapshot(client, "chain-a", validator, epoch=1, power=100_000)
    assert client.get(f"/evidence/{evidence['evidence_id']}").json()["status"] == "PENALIZED"
