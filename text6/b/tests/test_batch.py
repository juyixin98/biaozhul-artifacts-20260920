"""Batch import: 500-item cap, atomic rollback on any error, replay of a
repeat import.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

from tests.conftest import create_subject


def _grant_item(subject_id, purpose, expected_version=0, event_id=None):
    return {
        "event_id": event_id or f"b-{uuid.uuid4().hex[:12]}",
        "subject_id": subject_id,
        "purpose": purpose,
        "expected_version": expected_version,
        "policy_version": "v1",
        "event_type": "grant",
        "expires_at": (
            datetime.now(timezone.utc) + timedelta(days=1)
        ).isoformat(),
    }


def test_batch_imports_and_replays(client, admin_headers):
    s1 = create_subject(client, admin_headers)
    s2 = create_subject(client, admin_headers)
    batch = {"items": [
        _grant_item(s1["id"], "p1"),
        _grant_item(s1["id"], "p2"),
        _grant_item(s2["id"], "p3"),
    ]}
    r = client.post("/v1/consent/batch", headers=admin_headers, json=batch)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["imported"] == 3
    assert body["replayed"] == 0

    # Re-run identical batch: every item replays, no duplicates.
    r2 = client.post("/v1/consent/batch", headers=admin_headers, json=batch)
    assert r2.status_code == 200
    body2 = r2.json()
    assert body2["imported"] == 0
    assert body2["replayed"] == 3
    assert all(item["replayed"] for item in body2["items"])

    for sid in (s1["id"], s2["id"]):
        history = client.get(
            f"/v1/subjects/{sid}/history", headers=admin_headers
        ).json()
        # Still one event per stream -- replays did not insert.
        assert len(history) == len(
            [i for i in batch["items"] if i["subject_id"] == sid]
        )


def test_batch_over_500_rejected(client, admin_headers):
    subject = create_subject(client, admin_headers)
    items = [_grant_item(subject["id"], f"p{i}") for i in range(501)]
    r = client.post(
        "/v1/consent/batch", headers=admin_headers, json={"items": items}
    )
    assert r.status_code == 422


def test_batch_rolls_back_entire_batch_on_error(client, admin_headers):
    subject = create_subject(client, admin_headers)
    good = [_grant_item(subject["id"], f"p{i}") for i in range(3)]
    # 4th item references a nonexistent subject -> semantic failure.
    bad = _grant_item(999_999_999, "p3")
    r = client.post(
        "/v1/consent/batch",
        headers=admin_headers,
        json={"items": good + [bad]},
    )
    assert r.status_code in (404, 422), r.text

    # None of the good items survived: whole batch rolled back.
    history = client.get(
        f"/v1/subjects/{subject['id']}/history", headers=admin_headers
    ).json()
    assert history == []


def test_batch_duplicate_event_id_within_batch_conflicts(client, admin_headers):
    subject = create_subject(client, admin_headers)
    dup = f"b-{uuid.uuid4().hex[:12]}"
    items = [
        _grant_item(subject["id"], "p1", event_id=dup),
        _grant_item(subject["id"], "p2", event_id=dup),
    ]
    r = client.post(
        "/v1/consent/batch", headers=admin_headers, json={"items": items}
    )
    assert r.status_code == 409
    assert client.get(
        f"/v1/subjects/{subject['id']}/history", headers=admin_headers
    ).json() == []


def test_batch_same_event_id_different_content_conflicts(client, admin_headers):
    subject = create_subject(client, admin_headers)
    item = _grant_item(subject["id"], "p1")
    r1 = client.post(
        "/v1/consent/batch", headers=admin_headers, json={"items": [item]}
    )
    assert r1.status_code == 200

    # Same event_id, different purpose in the second import -> conflict, and
    # the accompanying fresh item must roll back with it.
    mutated = dict(item, purpose="different")
    other = _grant_item(subject["id"], "p2")
    r2 = client.post(
        "/v1/consent/batch",
        headers=admin_headers,
        json={"items": [mutated, other]},
    )
    assert r2.status_code == 409
    history = client.get(
        f"/v1/subjects/{subject['id']}/history", headers=admin_headers
    ).json()
    assert [e["purpose"] for e in history] == ["p1"]


def test_batch_multi_version_stream(client, admin_headers):
    """grant then withdraw on the same stream inside one batch using the
    expected in-batch version."""
    subject = create_subject(client, admin_headers)
    g = _grant_item(subject["id"], "lifecycle")
    w = {
        "event_id": f"b-{uuid.uuid4().hex[:12]}",
        "subject_id": subject["id"],
        "purpose": "lifecycle",
        "expected_version": 1,
        "event_type": "withdraw",
    }
    r = client.post(
        "/v1/consent/batch", headers=admin_headers, json={"items": [g, w]}
    )
    assert r.status_code == 200, r.text
    verdict = client.get(
        f"/v1/consent/{subject['id']}/lifecycle/verify", headers=admin_headers
    ).json()
    assert verdict["valid"] is False
    assert verdict["reason"] == "withdrawn"
    assert verdict["current_version"] == 2
