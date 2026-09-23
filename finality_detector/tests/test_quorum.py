"""Quorum arithmetic: strictly-more-than-2/3, integer-only comparisons."""


def test_one_vote_short_of_quorum(harness):
    """Weights 34/33/33 (total 100): 67 is required, 66 must NOT finalize."""
    h = harness({"alice": 34, "bob": 33, "carol": 33})
    assert h.create_epoch(0, 10).status_code == 201

    assert h.vote(0, 10, "alice", "X").status_code == 200
    resp = h.vote(0, 10, "bob", "X")
    assert resp.status_code == 200

    status = h.status(0)
    assert status["powers"] == {"X": 67}  # 34 + 33
    # 3*67 = 201 > 2*100 = 200  ->  this IS final. One vote short means 66.
    assert status["finalized_value"] == "X"


def test_exactly_two_thirds_is_not_final(harness):
    """Weights 2/2/2 (total 6): power 4 is exactly 2/3 -> NOT final; 5 is."""
    h = harness({"alice": 2, "bob": 2, "carol": 2})
    assert h.create_epoch(0, 10).status_code == 201

    h.vote(0, 10, "alice", "X")
    resp = h.vote(0, 10, "bob", "X")
    assert resp.status_code == 200
    status = h.status(0)
    assert status["powers"] == {"X": 4}
    # 3*4 = 12, 2*6 = 12 -> strictly-greater fails -> not final.
    assert status["finalized_value"] is None

    h.vote(0, 10, "carol", "X")
    status = h.status(0)
    assert status["powers"] == {"X": 6}
    assert status["finalized_value"] == "X"


def test_zero_weight_validators_cannot_finalize(harness):
    """Zero-weight votes verify and are stored, but move no weight."""
    h = harness({"alice": 0, "bob": 0, "carol": 1})
    assert h.create_epoch(0, 10).status_code == 201

    assert h.vote(0, 10, "alice", "X").status_code == 200
    assert h.vote(0, 10, "bob", "X").status_code == 200
    status = h.status(0)
    assert status["powers"] == {"X": 0}
    assert status["finalized_value"] is None

    # The single unit of weight is still not a quorum (3*1 > 2*1 is true,
    # so carol alone DOES finalize a 1-weight epoch).
    h.vote(0, 10, "carol", "X")
    status = h.status(0)
    assert status["finalized_value"] == "X"


def test_all_zero_weight_epoch_never_finalizes(harness):
    h = harness({"alice": 0, "bob": 0})
    assert h.create_epoch(0, 10).status_code == 201
    h.vote(0, 10, "alice", "X")
    h.vote(0, 10, "bob", "X")
    status = h.status(0)
    assert status["total_weight"] == 0
    assert status["finalized_value"] is None


def test_duplicate_same_value_counts_once(harness):
    h = harness({"alice": 34, "bob": 33, "carol": 33})
    h.create_epoch(0, 10)
    h.vote(0, 10, "alice", "X")
    resp = h.vote(0, 10, "alice", "X")  # same validator, same value again
    assert resp.status_code == 200
    assert resp.json()["duplicate"] is True
    status = h.status(0)
    assert status["powers"] == {"X": 34}  # still counted exactly once
    assert status["finalized_value"] is None
