"""Batch import: max 500, idempotent, strictly all-or-nothing."""

import uuid


def _eid():
    return f"evt-{uuid.uuid4()}"


def test_batch_imports_all_when_valid(client, org, purpose, policy_v1):
    h = org["admin"]
    events = [
        {"event_id": _eid(), "expected_version": 0, "subject_ref": f"u{i}",
         "action": "grant", "purpose_key": "marketing", "policy_version": 1}
        for i in range(3)
    ]
    r = client.post("/api/v1/events/batch", json={"events": events}, headers=h)
    assert r.status_code == 200, r.text
    assert r.json()["imported"] == 3
    for i in range(3):
        v = client.get(f"/api/v1/verify?subject_ref=u{i}&purpose_key=marketing",
                       headers=h).json()
        assert v["valid"] is True


def test_batch_any_error_rolls_back_everything(client, org, purpose, policy_v1):
    h = org["admin"]
    events = [
        {"event_id": _eid(), "expected_version": 0, "subject_ref": "good-1",
         "action": "grant", "purpose_key": "marketing", "policy_version": 1},
        {"event_id": _eid(), "expected_version": 0, "subject_ref": "good-2",
         "action": "grant", "purpose_key": "marketing", "policy_version": 1},
        # Bad: impossible expected version for a brand-new subject.
        {"event_id": _eid(), "expected_version": 99, "subject_ref": "bad",
         "action": "grant", "purpose_key": "marketing", "policy_version": 1},
    ]
    r = client.post("/api/v1/events/batch", json={"events": events}, headers=h)
    assert r.status_code == 409
    assert r.json()["error"] == "version_conflict"

    # Earlier items in the same batch must have rolled back too.
    for ref in ("good-1", "good-2"):
        v = client.get(f"/api/v1/verify?subject_ref={ref}&purpose_key=marketing",
                       headers=h).json()
        assert v["status"] == "no_record"


def test_batch_duplicate_event_id_within_batch_rejected(client, org, purpose, policy_v1):
    h = org["admin"]
    shared = _eid()
    events = [
        {"event_id": shared, "expected_version": 0, "subject_ref": "u1",
         "action": "grant", "purpose_key": "marketing", "policy_version": 1},
        {"event_id": shared, "expected_version": 0, "subject_ref": "u2",
         "action": "grant", "purpose_key": "marketing", "policy_version": 1},
    ]
    r = client.post("/api/v1/events/batch", json={"events": events}, headers=h)
    assert r.status_code == 409
    assert r.json()["error"] == "duplicate_in_batch"
    assert client.get("/api/v1/verify?subject_ref=u1&purpose_key=marketing",
                      headers=h).json()["status"] == "no_record"


def test_batch_rejects_more_than_500(client, org, purpose, policy_v1):
    h = org["admin"]
    events = [
        {"event_id": _eid(), "expected_version": 0, "subject_ref": f"u{i}",
         "action": "grant", "purpose_key": "marketing", "policy_version": 1}
        for i in range(501)
    ]
    r = client.post("/api/v1/events/batch", json={"events": events}, headers=h)
    assert r.status_code == 422


def test_batch_supports_mixed_grant_and_withdrawal_in_one_transaction(
    client, org, purpose, policy_v1
):
    h = org["admin"]
    events = [
        # grant for u1 (v0 -> v1)
        {"event_id": _eid(), "expected_version": 0, "subject_ref": "u1",
         "action": "grant", "purpose_key": "marketing", "policy_version": 1},
        # then withdraw u1 in the same batch (v1 -> v2)
        {"event_id": _eid(), "expected_version": 1, "subject_ref": "u1",
         "action": "withdrawal", "purpose_key": "marketing"},
    ]
    r = client.post("/api/v1/events/batch", json={"events": events}, headers=h)
    assert r.status_code == 200, r.text
    assert r.json()["imported"] == 2
    v = client.get("/api/v1/verify?subject_ref=u1&purpose_key=marketing",
                   headers=h).json()
    assert v["valid"] is False and v["reason"] == "withdrawn"
    assert v["state_version"] == 2


def test_batch_is_idempotent_on_retry(client, org, purpose, policy_v1):
    h = org["admin"]
    events = [
        {"event_id": _eid(), "expected_version": 0, "subject_ref": "u1",
         "action": "grant", "purpose_key": "marketing", "policy_version": 1},
        {"event_id": _eid(), "expected_version": 0, "subject_ref": "u2",
         "action": "grant", "purpose_key": "marketing", "policy_version": 1},
    ]
    r1 = client.post("/api/v1/events/batch", json={"events": events}, headers=h)
    assert r1.json()["imported"] == 2
    r2 = client.post("/api/v1/events/batch", json={"events": events}, headers=h)
    assert r2.status_code == 200
    # Second run replays both originals -> zero newly imported events.
    assert r2.json()["imported"] == 0
