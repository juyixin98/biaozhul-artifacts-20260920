"""Epoch isolation: signatures and tallies never leak across epochs."""


def test_signature_from_other_epoch_is_rejected(harness):
    h = harness({"alice": 40, "bob": 40, "carol": 20})
    h.create_epoch(0, 10)
    h.create_epoch(1, 20)

    body = h.vote_body(0, 10, "alice", "A")  # signed for epoch 0
    body["epoch_id"] = 1                     # replayed into epoch 1
    body["height"] = 20
    resp = h.client.post("/votes", json=body)
    assert resp.status_code == 400
    assert resp.json()["error"] == "bad_signature"


def test_tallies_are_independent_per_epoch(harness):
    h = harness({"alice": 40, "bob": 40, "carol": 20})
    h.create_epoch(0, 10)
    h.create_epoch(1, 20)

    h.vote(0, 10, "alice", "A")
    h.vote(0, 10, "bob", "A")
    h.vote(0, 10, "carol", "A")  # epoch 0 finalizes A with 100

    h.vote(1, 20, "alice", "B")  # epoch 1 sees only its own votes
    s0, s1 = h.status(0), h.status(1)
    assert s0["finalized_value"] == "A"
    assert s1["powers"] == {"B": 40}
    assert s1["finalized_value"] is None


def test_epoch_set_is_frozen_at_creation(harness):
    h = harness({"alice": 40, "bob": 40, "carol": 20})
    h.create_epoch(0, 10)
    # Recreating the same epoch (even with an identical set) is refused.
    resp = h.create_epoch(0, 10)
    assert resp.status_code == 409
    assert resp.json()["error"] == "epoch_exists"


def test_unknown_epoch_and_unknown_validator(harness):
    h = harness({"alice": 40, "bob": 40, "carol": 20})
    h.create_epoch(0, 10)

    resp = h.vote(9, 10, "alice", "A")
    assert resp.status_code == 404

    body = h.vote_body(0, 10, "alice", "A")
    body["validator_id"] = "mallory"
    resp = h.client.post("/votes", json=body)
    assert resp.status_code == 403
    assert resp.json()["error"] == "not_validator"


def test_height_mismatch_rejected(harness):
    h = harness({"alice": 40, "bob": 40, "carol": 20})
    h.create_epoch(0, 10)
    resp = h.vote(0, 11, "alice", "A")
    assert resp.status_code == 422
    assert resp.json()["error"] == "height_mismatch"


def test_bad_signature_rejected(harness):
    h = harness({"alice": 40, "bob": 40, "carol": 20})
    h.create_epoch(0, 10)
    body = h.vote_body(0, 10, "alice", "A")
    body["signature"] = body["signature"][:-4] + "AAAA"  # corrupt the signature
    resp = h.client.post("/votes", json=body)
    assert resp.status_code == 400
    assert resp.json()["error"] == "bad_signature"
