"""Grant / withdraw / idempotency / policy-version semantics via HTTP."""

import uuid


def _eid() -> str:
    return f"evt-{uuid.uuid4()}"


def test_grant_verify_withdraw_cycle(client, org, purpose, policy_v1):
    h = org["admin"]
    subject = "user-1"

    # No record initially.
    r = client.get(f"/api/v1/verify?subject_ref={subject}&purpose_key=marketing", headers=h)
    assert r.status_code == 200
    body = r.json()
    assert body["valid"] is False and body["status"] == "no_record"

    # Grant.
    payload = {"event_id": _eid(), "expected_version": 0, "subject_ref": subject,
               "purpose_key": "marketing", "policy_version": 1}
    r = client.post("/api/v1/events/grant", json=payload, headers=h)
    assert r.status_code == 200, r.text
    grant = r.json()
    assert grant["valid"] is True
    assert grant["basis"]["policy_version"] == 1
    assert grant["basis"]["event_type"] == "grant"

    # Verify reflects the grant.
    r = client.get(f"/api/v1/verify?subject_ref={subject}&purpose_key=marketing", headers=h)
    assert r.json()["valid"] is True
    assert r.json()["basis"]["grant_event_sequence"] == 1

    # Withdraw.
    w = {"event_id": _eid(), "expected_version": 1, "subject_ref": subject,
         "purpose_key": "marketing"}
    r = client.post("/api/v1/events/withdrawal", json=w, headers=h)
    assert r.status_code == 200
    assert r.json()["valid"] is False

    r = client.get(f"/api/v1/verify?subject_ref={subject}&purpose_key=marketing", headers=h)
    assert r.json()["valid"] is False
    assert r.json()["reason"] == "withdrawn"


def test_idempotent_retry_returns_original_result(client, org, purpose, policy_v1):
    h = org["admin"]
    payload = {"event_id": _eid(), "expected_version": 0, "subject_ref": "u9",
               "purpose_key": "marketing", "policy_version": 1}
    r1 = client.post("/api/v1/events/grant", json=payload, headers=h)
    assert r1.status_code == 200
    r2 = client.post("/api/v1/events/grant", json=payload, headers=h)
    assert r2.status_code == 200
    assert r2.json()["replayed"] is True
    # Same basis event sequence => the retry did not create a new history row.
    assert r1.json()["basis"] == r2.json()["basis"]


def test_same_event_id_different_body_conflicts(client, org, purpose, policy_v1):
    h = org["admin"]
    eid = _eid()
    payload = {"event_id": eid, "expected_version": 0, "subject_ref": "u9",
               "purpose_key": "marketing", "policy_version": 1}
    assert client.post("/api/v1/events/grant", json=payload, headers=h).status_code == 200
    payload["policy_version"] = 1  # identical value, sanity check
    changed = dict(payload, expected_version=0)
    changed["subject_ref"] = "different-user"
    r = client.post("/api/v1/events/grant", json=changed, headers=h)
    assert r.status_code == 409
    assert r.json()["error"] == "idempotency_mismatch"


def test_stale_expected_version_conflicts(client, org, purpose, policy_v1):
    h = org["admin"]
    payload = {"event_id": _eid(), "expected_version": 0, "subject_ref": "u9",
               "purpose_key": "marketing", "policy_version": 1}
    client.post("/api/v1/events/grant", json=payload, headers=h)

    stale = {"event_id": _eid(), "expected_version": 0, "subject_ref": "u9",
             "purpose_key": "marketing", "policy_version": 1}
    r = client.post("/api/v1/events/grant", json=stale, headers=h)
    assert r.status_code == 409
    assert r.json()["error"] == "version_conflict"


def test_withdrawal_retry_does_not_resurrect_and_new_grant_restores(
    client, org, purpose, policy_v1
):
    h = org["admin"]
    client.post("/api/v1/events/grant",
                json={"event_id": _eid(), "expected_version": 0, "subject_ref": "u9",
                      "purpose_key": "marketing", "policy_version": 1}, headers=h)
    w = {"event_id": _eid(), "expected_version": 1, "subject_ref": "u9",
         "purpose_key": "marketing"}
    r1 = client.post("/api/v1/events/withdrawal", json=w, headers=h)
    assert r1.json()["valid"] is False
    # Delayed retry of the same withdrawal: replays original, stays withdrawn.
    r2 = client.post("/api/v1/events/withdrawal", json=w, headers=h)
    assert r2.json()["replayed"] is True and r2.json()["valid"] is False

    # A new explicit grant at the current version restores consent.
    r3 = client.post(
        "/api/v1/events/grant",
        json={"event_id": _eid(), "expected_version": 2, "subject_ref": "u9",
              "purpose_key": "marketing", "policy_version": 1}, headers=h,
    )
    assert r3.status_code == 200 and r3.json()["valid"] is True


def test_delayed_grant_retry_after_withdrawal_does_not_restore(
    client, org, purpose, policy_v1
):
    h = org["admin"]
    grant = {"event_id": _eid(), "expected_version": 0, "subject_ref": "u9",
             "purpose_key": "marketing", "policy_version": 1}
    client.post("/api/v1/events/grant", json=grant, headers=h)
    client.post("/api/v1/events/withdrawal",
                json={"event_id": _eid(), "expected_version": 1, "subject_ref": "u9",
                      "purpose_key": "marketing"}, headers=h)

    # A delayed retry of the *old grant* returns its stored result but never
    # flips the projection back to granted.
    r = client.post("/api/v1/events/grant", json=grant, headers=h)
    assert r.status_code == 200
    assert r.json()["replayed"] is True
    verify = client.get(
        "/api/v1/verify?subject_ref=u9&purpose_key=marketing", headers=h
    ).json()
    assert verify["valid"] is False
    assert verify["reason"] == "withdrawn"
    assert verify["state_version"] == 2


def test_policy_publication_is_versioned_and_grant_not_renewed(client, org, purpose):
    h = org["admin"]
    r = client.post(f"/api/v1/purposes/{purpose}/policy-versions",
                    json={"body": "v1"}, headers=h)
    assert r.json()["version"] == 1
    r = client.post(f"/api/v1/purposes/{purpose}/policy-versions",
                    json={"body": "v2"}, headers=h)
    assert r.json()["version"] == 2

    client.post("/api/v1/events/grant",
                json={"event_id": _eid(), "expected_version": 0, "subject_ref": "u9",
                      "purpose_key": "marketing", "policy_version": 1}, headers=h)
    r = client.get("/api/v1/verify?subject_ref=u9&purpose_key=marketing", headers=h)
    # New policy version published, but the grant stays bound to v1.
    assert r.json()["valid"] is True
    assert r.json()["basis"]["policy_version"] == 1

    # Granting against a non-existent version is rejected; no event is written.
    r = client.post(
        "/api/v1/events/grant",
        json={"event_id": _eid(), "expected_version": 1, "subject_ref": "u9",
              "purpose_key": "marketing", "policy_version": 99}, headers=h,
    )
    assert r.status_code == 422
    assert r.json()["error"] == "unknown_policy_version"


def test_failed_write_leaves_no_history(client, org, purpose, policy_v1):
    h = org["admin"]
    # Invalid: withdrawal on a subject with no record -> conflict, no event.
    r = client.post("/api/v1/events/withdrawal",
                    json={"event_id": _eid(), "expected_version": 0,
                          "subject_ref": "ghost", "purpose_key": "marketing"}, headers=h)
    assert r.status_code == 409
    r = client.get("/api/v1/history?subject_ref=ghost&purpose_key=marketing", headers=h)
    assert r.status_code == 404  # subject mapping was never persisted


def test_history_is_ordered_and_pseudonymous(client, org, purpose, policy_v1):
    h = org["admin"]
    client.post("/api/v1/events/grant",
                json={"event_id": _eid(), "expected_version": 0, "subject_ref": "u9",
                      "purpose_key": "marketing", "policy_version": 1}, headers=h)
    client.post("/api/v1/events/withdrawal",
                json={"event_id": _eid(), "expected_version": 1, "subject_ref": "u9",
                      "purpose_key": "marketing"}, headers=h)
    r = client.get("/api/v1/history?subject_ref=u9&purpose_key=marketing", headers=h)
    seq = [(e["sequence"], e["event_type"]) for e in r.json()]
    assert seq == [(1, "grant"), (2, "withdrawal")]
    assert all("subject_ref" not in e for e in r.json())
