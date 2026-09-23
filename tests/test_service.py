"""End-to-end tests via the FastAPI TestClient against PostgreSQL."""
from __future__ import annotations

from conftest import add_snapshot, register_chain_and_validator, signed_vote


# ------------------------------------------------------------- happy path / dup

def test_single_vote_no_conflict(client, validator):
    register_chain_and_validator(client, "chain-a", validator)
    r = client.post("/votes", json=signed_vote(
        validator, chain_id="chain-a", height=10, round=0, vote_type="prevote"))
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "accepted_no_conflict"


def test_two_identical_votes_are_not_double_sign(client, validator):
    register_chain_and_validator(client, "chain-a", validator)
    vote = signed_vote(validator, chain_id="chain-a", height=10, round=0,
                       vote_type="prevote", block_hash="f" * 64)
    assert client.post("/votes", json=vote).json()["status"] == "accepted_no_conflict"
    second = client.post("/votes", json=vote)
    assert second.status_code == 200
    assert second.json()["status"] == "duplicate_vote"
    assert client.get("/penalties").json()["penalties"] == []
    assert client.get("/evidence").json()["evidence"] == []


def test_double_sign_slash_once_and_evidence_preserved(client, validator):
    register_chain_and_validator(client, "chain-a", validator)
    add_snapshot(client, "chain-a", validator, epoch=0, power=1_000_000)

    v1 = signed_vote(validator, chain_id="chain-a", height=10, round=0,
                     vote_type="prevote", block_hash="a" * 64)
    v2 = signed_vote(validator, chain_id="chain-a", height=10, round=0,
                     vote_type="prevote", block_hash="b" * 64)

    first = client.post("/votes", json=v1)
    assert first.json()["status"] == "accepted_no_conflict"
    second = client.post("/votes", json=v2)
    body = second.json()
    assert body["status"] == "penalized", body
    assert body["detail"].startswith("slashed 10000 of 1000000")
    evidence_id = body["evidence_id"]

    # Raw evidence and judgment version are retrievable verbatim.
    detail = client.get(f"/evidence/{evidence_id}").json()
    assert detail["status"] == "PENALIZED"
    assert detail["judgment_version"] == "rules-1.0.0"
    assert len(detail["raw_evidence"]["votes"]) == 2
    assert detail["raw_evidence"]["votes"][0]["block_hash"] == "a" * 64
    assert detail["penalty"]["slashed_power"] == 10_000
    assert detail["penalty"]["frozen_power"] == 1_000_000
    assert detail["penalty"]["snapshot_id"] is not None


# --------------------------------------------------------------- reverse order

def test_reverse_order_identity_and_single_penalty(client, validator):
    register_chain_and_validator(client, "chain-a", validator)
    add_snapshot(client, "chain-a", validator, epoch=0, power=800_000)

    h = "33" * 32
    v1 = signed_vote(validator, chain_id="chain-a", height=9, round=2,
                     vote_type="prevote", block_hash="aa" * 32)
    v2 = signed_vote(validator, chain_id="chain-a", height=9, round=2,
                     vote_type="prevote", block_hash="bb" * 32)

    # Order 1: v1 then v2
    client.post("/votes", json=v1)
    id_forward = client.post("/votes", json=v2).json()["evidence_id"]

    # Reset DB and do order 2: v2 then v1
    from app.db import get_pool
    with get_pool().connection() as conn:
        for t in ["penalties", "evidence", "votes"]:
            conn.execute(f"TRUNCATE TABLE {t} RESTART IDENTITY CASCADE")
        conn.commit()

    client.post("/votes", json=v2)
    id_reverse = client.post("/votes", json=v1).json()["evidence_id"]
    assert id_forward == id_reverse

    penalties = client.get("/penalties").json()["penalties"]
    assert len(penalties) == 1
    assert penalties[0]["evidence_id"] == id_forward


def test_late_third_and_repeats_punish_only_once(client, validator):
    register_chain_and_validator(client, "chain-a", validator)
    add_snapshot(client, "chain-a", validator, epoch=0, power=700_000)
    votes = [
        signed_vote(validator, chain_id="chain-a", height=3, round=1,
                    vote_type="prevote", block_hash=c * 64)
        for c in "abc"
    ]
    client.post("/votes", json=votes[0])
    eid = client.post("/votes", json=votes[1]).json()["evidence_id"]

    # Third distinct block hash (late arrival), then repeated inputs.
    for payload in (votes[2], votes[0], votes[1]):
        r = client.post("/votes", json=payload)
        assert r.status_code == 200
        assert r.json()["evidence_id"] == eid
        assert r.json()["status"] == "already_penalized", r.json()

    assert len(client.get("/penalties").json()["penalties"]) == 1
    assert len(client.get("/evidence").json()["evidence"]) == 1


# --------------------------------------------------- boundaries & vote typing

def test_boundary_rounds_freeze_correct_epoch_snapshot(client, validator):
    """Rounds 9 -> epoch 0 snapshot, round 10 -> epoch 1 snapshot."""
    register_chain_and_validator(client, "chain-a", validator)
    add_snapshot(client, "chain-a", validator, epoch=0, power=100_000)
    add_snapshot(client, "chain-a", validator, epoch=1, power=900_000)

    def slash_round(round_index, block_hashes):
        vs = [
            signed_vote(validator, chain_id="chain-a", height=100, round=round_index,
                        vote_type="prevote", block_hash=bh)
            for bh in block_hashes
        ]
        for v in vs:
            client.post("/votes", json=v)
        ev = client.get("/evidence").json()["evidence"]
        return [e for e in ev if e["round"] == round_index][0]

    e0 = slash_round(9, ("01" * 32, "02" * 32))
    e1 = slash_round(10, ("03" * 32, "04" * 32))
    assert e0["epoch"] == 0
    assert e1["epoch"] == 1

    penalties = {p["epoch"]: p for p in client.get("/penalties").json()["penalties"]}
    assert penalties[0]["frozen_power"] == 100_000
    assert penalties[0]["slashed_power"] == 1_000
    assert penalties[1]["frozen_power"] == 900_000
    assert penalties[1]["slashed_power"] == 9_000
    assert penalties[0]["snapshot_id"] != penalties[1]["snapshot_id"]


def test_prevote_and_precommit_are_separate_offenses(client, validator):
    register_chain_and_validator(client, "chain-a", validator)
    add_snapshot(client, "chain-a", validator, epoch=0, power=200_000)
    for vt in ("prevote", "precommit"):
        for bh in ("aa" * 32, "bb" * 32):
            client.post("/votes", json=signed_vote(
                validator, chain_id="chain-a", height=4, round=0,
                vote_type=vt, block_hash=bh))
    evidence = client.get("/evidence").json()["evidence"]
    assert {e["vote_type"] for e in evidence} == {"prevote", "precommit"}
    assert len(client.get("/penalties").json()["penalties"]) == 2


# ------------------------------------------------------- security: bad inputs

def test_bad_signature_rejected(client, validator):
    register_chain_and_validator(client, "chain-a", validator)
    vote = signed_vote(validator, chain_id="chain-a", height=1, round=0, vote_type="prevote")
    vote["signature"] = "00" * 64
    r = client.post("/votes", json=vote)
    assert r.status_code == 422
    assert "signature verification failed" in r.json()["detail"]


def test_cross_chain_replay_rejected(client, validator):
    register_chain_and_validator(client, "chain-a", validator)
    register_chain_and_validator(client, "chain-b", validator)
    vote_a = signed_vote(validator, chain_id="chain-a", height=1, round=0, vote_type="prevote")
    # Replay the exact signed bytes on chain B without re-signing.
    vote_b = {**vote_a, "chain_id": "chain-b"}
    r = client.post("/votes", json=vote_b)
    assert r.status_code == 422
    assert "signature verification failed" in r.json()["detail"]


def test_tampered_block_hash_rejected(client, validator):
    register_chain_and_validator(client, "chain-a", validator)
    vote = signed_vote(validator, chain_id="chain-a", height=1, round=0,
                       vote_type="prevote", block_hash="ab" * 32)
    r = client.post("/votes", json={**vote, "block_hash": "cd" * 32})
    assert r.status_code == 422


def test_unknown_chain_and_validator_rejected(client, validator):
    vote = signed_vote(validator, chain_id="ghost", height=1, round=0, vote_type="prevote")
    assert client.post("/votes", json=vote).status_code == 422


# ----------------------------------------------------- epoch freezing semantics

def test_later_delegation_cannot_change_historical_penalty_base(client, validator):
    register_chain_and_validator(client, "chain-a", validator)
    add_snapshot(client, "chain-a", validator, epoch=0, power=100_000)

    for bh in ("01" * 32, "02" * 32):
        client.post("/votes", json=signed_vote(
            validator, chain_id="chain-a", height=5, round=0,
            vote_type="prevote", block_hash=bh))

    penalty_before = client.get("/penalties").json()["penalties"][0]
    assert penalty_before["frozen_power"] == 100_000

    # A later delegation attempts to rewrite history: snapshot is frozen.
    r = client.post("/snapshots", json={
        "chain_id": "chain-a",
        "validator_address": validator["validator_address"],
        "epoch": 0, "voting_power": 999_000_000,
    })
    assert r.status_code == 409

    # Resubmitting identical frozen value is a harmless no-op.
    ok = client.post("/snapshots", json={
        "chain_id": "chain-a",
        "validator_address": validator["validator_address"],
        "epoch": 0, "voting_power": 100_000,
    })
    assert ok.status_code == 201

    penalty_after = client.get("/penalties").json()["penalties"][0]
    assert penalty_after == penalty_before


def test_evidence_waits_for_snapshot_then_auto_penalizes(client, validator):
    """Double sign arrives before the epoch snapshot exists."""
    register_chain_and_validator(client, "chain-a", validator)
    for bh in ("01" * 32, "02" * 32):
        r = client.post("/votes", json=signed_vote(
            validator, chain_id="chain-a", height=8, round=15,
            vote_type="prevote", block_hash=bh))
    assert r.json()["status"] == "awaiting_snapshot"
    eid = r.json()["evidence_id"]
    assert client.get(f"/evidence/{eid}").json()["status"] == "AWAITING_SNAPSHOT"
    assert client.get("/penalties").json()["penalties"] == []

    # Snapshot for epoch 1 (round 15 / 10) arrives late -> auto-reconciled.
    add_snapshot(client, "chain-a", validator, epoch=1, power=333_000)
    detail = client.get(f"/evidence/{eid}").json()
    assert detail["status"] == "PENALIZED"
    assert detail["penalty"]["frozen_power"] == 333_000
    assert detail["penalty"]["slashed_power"] == 3_330
