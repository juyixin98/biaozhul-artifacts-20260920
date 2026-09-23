"""Finality conflicts: alarm raised, epoch frozen, finalized value immutable."""

from app import crypto

CHAIN_ID = "test-chain"


def _finalize(h, epoch, height, value, voters):
    for vid in voters:
        resp = h.vote(epoch, height, vid, value)
        assert resp.status_code == 200
    status = h.status(epoch)
    assert status["finalized_value"] == value


def test_conflicting_certificate_raises_alarm_and_freezes(harness):
    h = harness({"alice": 40, "bob": 40, "carol": 20})
    h.create_epoch(0, 10)
    _finalize(h, 0, 10, "A", ["alice", "bob"])

    # A conflicting quorum certificate for B signed by 80 weight arrives.
    cert = {
        "epoch_id": 0,
        "height": 10,
        "value": "B",
        "signatures": [
            {"validator_id": vid, "signature": crypto.sign_vote(h.keys[vid], CHAIN_ID, 0, 10, "B")}
            for vid in ("alice", "bob")
        ],
    }
    resp = h.client.post("/certificates", json=cert)
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "finality_conflict"
    assert body["frozen"] is True

    # The finalized value is NOT overwritten by the newer message.
    status = h.status(0)
    assert status["finalized_value"] == "A"
    assert status["frozen"] is True

    # The alarm carries the conflicting evidence.
    alarms = h.client.get("/alarms").json()["alarms"]
    assert len(alarms) == 1
    assert alarms[0]["kind"] == "finality_conflict"
    detail = alarms[0]["detail"]
    assert detail["finalized_value"] == "A"
    assert detail["conflicting_value"] == "B"
    assert detail["conflicting_power"] == 80
    assert len(detail["evidence"]) == 2


def test_frozen_epoch_refuses_votes_and_advancement(harness):
    h = harness({"alice": 40, "bob": 40, "carol": 20})
    h.create_epoch(0, 10)
    _finalize(h, 0, 10, "A", ["alice", "bob"])

    cert = {
        "epoch_id": 0,
        "height": 10,
        "value": "B",
        "signatures": [
            {"validator_id": vid, "signature": crypto.sign_vote(h.keys[vid], CHAIN_ID, 0, 10, "B")}
            for vid in ("alice", "bob")
        ],
    }
    h.client.post("/certificates", json=cert)

    # No more votes are processed on the frozen epoch.
    resp = h.vote(0, 10, "carol", "A")
    assert resp.status_code == 409
    assert resp.json()["error"] == "epoch_frozen"

    # The chain cannot advance past a frozen epoch.
    resp = h.create_epoch(1, 20)
    assert resp.status_code == 409
    assert resp.json()["error"] == "predecessor_frozen"


def test_certificate_below_quorum_does_not_alarm(harness):
    h = harness({"alice": 40, "bob": 40, "carol": 20})
    h.create_epoch(0, 10)
    _finalize(h, 0, 10, "A", ["alice", "bob"])

    cert = {
        "epoch_id": 0,
        "height": 10,
        "value": "B",
        "signatures": [
            {"validator_id": "carol", "signature": crypto.sign_vote(h.keys["carol"], CHAIN_ID, 0, 10, "B")}
        ],
    }
    resp = h.client.post("/certificates", json=cert)
    assert resp.status_code == 200
    assert resp.json()["status"] == "challenge_below_quorum"
    assert h.status(0)["frozen"] is False
    assert h.client.get("/alarms").json()["alarms"] == []


def test_agreeing_certificate_is_accepted(harness):
    h = harness({"alice": 40, "bob": 40, "carol": 20})
    h.create_epoch(0, 10)
    _finalize(h, 0, 10, "A", ["alice", "bob"])

    cert = {
        "epoch_id": 0,
        "height": 10,
        "value": "A",
        "signatures": [
            {"validator_id": vid, "signature": crypto.sign_vote(h.keys[vid], CHAIN_ID, 0, 10, "A")}
            for vid in ("alice", "bob", "carol")
        ],
    }
    resp = h.client.post("/certificates", json=cert)
    assert resp.status_code == 200
    assert resp.json()["status"] == "agreement"
    assert h.status(0)["finalized_value"] == "A"


def test_certificate_with_bad_signature_rejected(harness):
    h = harness({"alice": 40, "bob": 40, "carol": 20})
    h.create_epoch(0, 10)
    _finalize(h, 0, 10, "A", ["alice", "bob"])

    bad_sig = crypto.sign_vote(h.keys["alice"], CHAIN_ID, 0, 10, "B")
    bad_sig = bad_sig[:-4] + "AAAA"
    cert = {
        "epoch_id": 0,
        "height": 10,
        "value": "B",
        "signatures": [
            {"validator_id": "alice", "signature": bad_sig},
            {"validator_id": "bob", "signature": crypto.sign_vote(h.keys["bob"], CHAIN_ID, 0, 10, "B")},
        ],
    }
    resp = h.client.post("/certificates", json=cert)
    assert resp.status_code == 400
    assert resp.json()["error"] == "bad_signature"
    assert h.status(0)["frozen"] is False
