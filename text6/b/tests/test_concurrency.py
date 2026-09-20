"""Concurrent withdraw: exactly one writer wins, the other gets a 409 stale
version conflict; consent ends withdrawn and no failed event enters history.
"""
from __future__ import annotations

import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

from tests.conftest import create_subject, grant


def _withdraw(client, headers, subject_id, purpose, event_id):
    return client.post(
        "/v1/consent/withdraw",
        headers=headers,
        json={
            "event_id": event_id,
            "subject_id": subject_id,
            "purpose": purpose,
            "expected_version": 1,
        },
    )


def test_concurrent_withdraw_exactly_one_wins(client, admin_headers):
    subject = create_subject(client, admin_headers)
    purpose = "marketing"
    r, _ = grant(
        client,
        admin_headers,
        subject["id"],
        purpose,
        expires_at=datetime.now(timezone.utc) + timedelta(days=1),
    )
    assert r.status_code == 200, r.text

    # Two concurrent withdrawals, each believing it is appending version 2.
    ids = [f"w-{uuid.uuid4().hex[:10]}", f"w-{uuid.uuid4().hex[:10]}"]
    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(
            pool.map(
                lambda eid: _withdraw(client, admin_headers, subject["id"], purpose, eid),
                ids,
            )
        )

    statuses = sorted(resp.status_code for resp in responses)
    assert statuses == [200, 409], [(resp.status_code, resp.text) for resp in responses]

    winner = next(resp for resp in responses if resp.status_code == 200).json()
    assert winner["version"] == 2
    assert winner["event_type"] == "withdraw"

    # Final state: withdrawn, stream at version 2.
    verify = client.get(
        f"/v1/consent/{subject['id']}/{purpose}/verify", headers=admin_headers
    ).json()
    assert verify["valid"] is False
    assert verify["reason"] == "withdrawn"
    assert verify["current_version"] == 2

    # Only two events exist (one grant, one withdraw) -- the loser never
    # entered the ledger.
    history = client.get(
        f"/v1/subjects/{subject['id']}/history", headers=admin_headers
    ).json()
    assert [e["event_type"] for e in history] == ["grant", "withdraw"]


def test_concurrent_grant_after_withdraw_needs_new_version(client, admin_headers):
    """A concurrent stale grant cannot resurrect a withdrawn stream at the old
    version; only a new explicit grant at the new version restores consent."""
    subject = create_subject(client, admin_headers)
    purpose = "tracking"
    r, _ = grant(
        client,
        admin_headers,
        subject["id"],
        purpose,
        expires_at=datetime.now(timezone.utc) + timedelta(days=1),
    )
    assert r.status_code == 200

    wr = client.post(
        "/v1/consent/withdraw",
        headers=admin_headers,
        json={
            "event_id": f"w-{uuid.uuid4().hex[:10]}",
            "subject_id": subject["id"],
            "purpose": purpose,
            "expected_version": 1,
        },
    )
    assert wr.status_code == 200

    # Retry of the original grant id/request (identical content): replay,
    # state stays withdrawn. expected_version is a precondition, not content.
    original = r.json()
    r2, grant_id = grant(
        client,
        admin_headers,
        subject["id"],
        purpose,
        expected_version=2,
        policy_version=original["policy_version"],
        event_id=original["event_id"],
        expires_at=original["expires_at"],
    )
    assert r2.status_code == 200, r2.text
    assert r2.json()["replayed"] is True
    v = client.get(
        f"/v1/consent/{subject['id']}/{purpose}/verify", headers=admin_headers
    ).json()
    assert v["valid"] is False
    assert v["reason"] == "withdrawn"

    # A brand-new explicit grant at expected_version=2 restores consent.
    r3, _ = grant(
        client,
        admin_headers,
        subject["id"],
        purpose,
        expected_version=2,
        expires_at=datetime.now(timezone.utc) + timedelta(days=1),
    )
    assert r3.status_code == 200, r3.text
    v2 = client.get(
        f"/v1/consent/{subject['id']}/{purpose}/verify", headers=admin_headers
    ).json()
    assert v2["valid"] is True
    assert v2["policy_version"] == "v1"
