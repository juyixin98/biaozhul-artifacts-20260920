"""Equivocation (double voting): evidence kept, weight excluded by rule."""


def test_double_vote_excludes_validator_and_keeps_evidence(harness):
    h = harness({"alice": 40, "bob": 40, "carol": 20})
    h.create_epoch(0, 10)

    h.vote(0, 10, "alice", "A")
    h.vote(0, 10, "carol", "A")  # A has 60, not yet final (needs 67)
    resp = h.vote(0, 10, "bob", "B")
    assert resp.status_code == 200

    # bob signs A as well -> equivocation at the same (epoch, height).
    resp = h.vote(0, 10, "bob", "A")
    assert resp.status_code == 200
    body = resp.json()
    assert body["equivocation"] is True

    status = h.status(0)
    # bob's whole weight is excluded from BOTH values.
    assert status["powers"] == {"A": 60}
    assert status["excluded"] == ["bob"]
    assert status["finalized_value"] is None  # 60 < 67, exclusion prevented finality

    # Both signed messages are retrievable as evidence.
    evidence = h.client.get("/epochs/0/evidence").json()["exclusions"]
    assert len(evidence) == 1
    entry = evidence[0]
    assert entry["validator_id"] == "bob"
    assert entry["reason"].startswith("equivocation")
    signed = entry["signed_votes"]
    assert {v["value"] for v in signed} == {"A", "B"}
    assert all(len(v["signature"]) > 0 for v in signed)


def test_excluded_validator_stays_excluded_for_later_votes(harness):
    h = harness({"alice": 40, "bob": 40, "carol": 20})
    h.create_epoch(0, 10)
    h.vote(0, 10, "bob", "B")
    h.vote(0, 10, "bob", "A")  # equivocation -> excluded
    h.vote(0, 10, "bob", "A")  # any further vote is still ignored for tally
    status = h.status(0)
    assert status["powers"] == {}
    assert status["excluded"] == ["bob"]


def test_equivocation_does_not_cross_epochs(harness):
    """Equivocating in epoch 0 must not exclude the validator in epoch 1."""
    h = harness({"alice": 40, "bob": 40, "carol": 20})
    h.create_epoch(0, 10)
    h.create_epoch(1, 20)

    h.vote(0, 10, "bob", "A")
    h.vote(0, 10, "bob", "B")  # excluded in epoch 0 only

    h.vote(1, 20, "alice", "A")
    h.vote(1, 20, "bob", "A")
    status = h.status(1)
    assert status["powers"] == {"A": 80}
    assert status["excluded"] == []
    assert status["finalized_value"] == "A"
