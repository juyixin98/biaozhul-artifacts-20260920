"""Idempotency: duplicate event_ids return the original result; divergent reuse conflicts."""
from __future__ import annotations

from tests.conftest import auth


def _publish(client, org, body="policy v1"):
    r = client.post("/policies", json={"body": body}, headers=auth(org["admin"]))
    assert r.status_code == 201, r.text
    return r.json()["version"]


def _grant(client, org, **overrides):
    payload = {
        "event_id": "evt-1",
        "subject_key": "user-1",
        "purpose": "analytics",
        "action": "grant",
        "expected_version": 0,
    }
    payload.update(overrides)
    return client.post("/consents", json=payload, headers=auth(org["admin"]))


def test_duplicate_request_returns_original_result(client, org):
    _publish(client, org)
    first = _grant(client, org)
    assert first.status_code == 201, first.text
    assert first.json()["replayed"] is False

    second = _grant(client, org)
    assert second.status_code == 201, second.text
    body = second.json()
    assert body["replayed"] is True
    assert body["state_version"] == first.json()["state_version"]

    # Only one ledger row exists.
    r = client.get(
        "/subjects/user-1/events", headers=auth(org["admin"]), params={"purpose": "analytics"}
    )
    assert len(r.json()) == 1


def test_same_instant_different_offset_is_the_same_request(client, org):
    """Idempotency must compare absolute instants, not offset strings."""
    from datetime import datetime, timedelta, timezone

    version = _publish(client, org)
    expiry = datetime.now(timezone.utc) + timedelta(days=7)
    # First request expressed in UTC.
    first = client.post(
        "/consents",
        json={
            "event_id": "evt-tz",
            "subject_key": "u-tz",
            "purpose": "analytics",
            "action": "grant",
            "expected_version": 0,
            "policy_version": version,
            "expires_at": expiry.isoformat(),
        },
        headers=auth(org["admin"]),
    )
    assert first.status_code == 201, first.text

    # Replay the same instant in +08:00 — must be treated as identical.
    expiry_plus8 = expiry.astimezone(timezone(timedelta(hours=8)))
    replay = client.post(
        "/consents",
        json={
            "event_id": "evt-tz",
            "subject_key": "u-tz",
            "purpose": "analytics",
            "action": "grant",
            "expected_version": 0,
            "policy_version": version,
            "expires_at": expiry_plus8.isoformat(),
        },
        headers=auth(org["admin"]),
    )
    assert replay.status_code == 201, replay.text
    assert replay.json()["replayed"] is True


def test_same_event_id_different_payload_conflicts(client, org):
    _publish(client, org)
    ok = _grant(client, org)
    assert ok.status_code == 201, ok.text

    # Same event_id, different purpose => content conflict, never an overwrite.
    conflict = _grant(client, org, purpose="marketing")
    assert conflict.status_code == 409, conflict.text

    # The original ledger row is untouched.
    verify = client.get(
        "/subjects/user-1/consents/analytics", headers=auth(org["admin"])
    )
    assert verify.json()["valid"] is True


def test_withdraw_replay_after_later_withdraw_does_not_restore(client, org):
    """A delayed retry of an old grant/withdraw id must not resurrect consent.

    Sequence: grant(e1,v0) -> withdraw(e2,v1) -> retry e1 (same id) -> still
    withdrawn (e1 is simply replayed; only a *new explicit grant* restores).
    """
    _publish(client, org)
    assert _grant(client, org).status_code == 201

    w = _grant(
        client,
        org,
        event_id="evt-2",
        action="withdraw",
        expected_version=1,
    )
    assert w.status_code == 201, w.text

    # Late/duplicate retry of the original grant request.
    replay = _grant(client, org)
    assert replay.status_code == 201
    assert replay.json()["replayed"] is True

    state = client.get(
        "/subjects/user-1/consents/analytics", headers=auth(org["admin"])
    ).json()
    assert state["valid"] is False
    assert state["status"] == "withdrawn"
    assert state["basis_event_id"] == "evt-2"

    # A fresh explicit grant with the correct new expected version restores it.
    restore = _grant(
        client, org, event_id="evt-3", expected_version=2
    )
    assert restore.status_code == 201, restore.text
    again = client.get(
        "/subjects/user-1/consents/analytics", headers=auth(org["admin"])
    ).json()
    assert again["valid"] is True
    assert again["basis_event_id"] == "evt-3"
