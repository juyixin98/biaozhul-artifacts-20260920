"""End-to-end HTTP flow tests, including authorization and timeout reaping."""

from datetime import datetime, timedelta, timezone

from app.models import AssignmentStatus, TaskStatus

UTC = timezone.utc


def _setup(client):
    unit = client.post("/directory/units", params={"name": "North"}).json()
    other_unit = client.post("/directory/units", params={"name": "South"}).json()
    coord = client.post(
        "/directory/coordinators", params={"name": "Nora", "unit_ids": str(unit["id"])}
    ).json()
    outsider = client.post(
        "/directory/coordinators",
        params={"name": "Sam", "unit_ids": str(other_unit["id"])},
    ).json()
    w1 = client.post("/directory/workers", json={
        "name": "Alice", "unit_id": unit["id"], "timezone": "UTC"}).json()
    w2 = client.post("/directory/workers", json={
        "name": "Ben", "unit_id": unit["id"], "timezone": "UTC"}).json()
    client.post(f"/directory/workers/{w1['id']}/qualifications", json={
        "code": "personal_care",
        "valid_from": "2026-01-01T00:00:00Z",
        "valid_until": "2027-01-01T00:00:00Z",
    })
    client.post(f"/directory/workers/{w2['id']}/qualifications", json={
        "code": "personal_care",
        "valid_from": "2026-01-01T00:00:00Z",
        "valid_until": "2027-01-01T00:00:00Z",
    })
    return unit, coord, outsider, w1, w2


def _make_plan(client, coord, unit):
    return client.post(
        "/plans",
        headers={"X-Coordinator-ID": str(coord["id"])},
        json={
            "care_recipient_id": "r1",
            "unit_id": unit["id"],
            "service_timezone": "UTC",
            "recurrence": "daily",
            "window_start": "08:00:00",
            "window_end": "10:00:00",
            "duration_minutes": 60,
            "required_qualifications": ["personal_care"],
        },
    ).json()


def test_full_allocate_accept_flow(client):
    unit, coord, _, w1, _ = _setup(client)
    plan = _make_plan(client, coord, unit)
    generated = client.post(
        f"/plans/{plan['id']}/generate",
        headers={"X-Coordinator-ID": str(coord["id"])},
    ).json()
    assert generated["created"] == 14
    task = generated["tasks"][0]

    resp = client.post(
        f"/tasks/{task['id']}/allocate",
        headers={"X-Coordinator-ID": str(coord["id"])},
    )
    assert resp.status_code == 200
    result = resp.json()
    assert result["allocated"]
    assert result["assignment"]["worker_id"] == w1["id"]

    accept = client.post(
        f"/tasks/{task['id']}/accept",
        headers={"X-Worker-ID": str(w1["id"])},
        json={"request_key": "accept-1"},
    ).json()
    assert accept["accepted"]
    assert accept["assignment"]["status"] == AssignmentStatus.assigned.value

    # Duplicate request with same key replays, no second booking.
    replay = client.post(
        f"/tasks/{task['id']}/accept",
        headers={"X-Worker-ID": str(w1["id"])},
        json={"request_key": "accept-1"},
    ).json()
    assert replay["replayed"] is True


def test_allocate_requires_authorized_unit(client):
    unit, coord, outsider, _, _ = _setup(client)
    plan = _make_plan(client, coord, unit)
    generated = client.post(
        f"/plans/{plan['id']}/generate",
        headers={"X-Coordinator-ID": str(coord["id"])},
    ).json()
    task = generated["tasks"][0]

    resp = client.post(
        f"/tasks/{task['id']}/allocate",
        headers={"X-Coordinator-ID": str(outsider["id"])},
    )
    assert resp.status_code == 403


def test_plan_create_requires_unit_authorization(client):
    unit, coord, outsider, _, _ = _setup(client)
    resp = client.post(
        "/plans",
        headers={"X-Coordinator-ID": str(outsider["id"])},
        json={
            "care_recipient_id": "r1",
            "unit_id": unit["id"],
            "recurrence": "daily",
            "window_start": "08:00:00",
            "window_end": "10:00:00",
            "duration_minutes": 60,
        },
    )
    assert resp.status_code == 403


def test_allocate_409_returns_specific_violations(client):
    unit = client.post("/directory/units", params={"name": "U"}).json()
    coord = client.post(
        "/directory/coordinators", params={"name": "C", "unit_ids": str(unit["id"])}
    ).json()
    client.post("/directory/workers", json={
        "name": "Unqualified Worker", "unit_id": unit["id"]})
    plan = client.post("/plans", headers={"X-Coordinator-ID": str(coord["id"])}, json={
        "care_recipient_id": "r", "unit_id": unit["id"],
        "recurrence": "daily", "window_start": "08:00:00",
        "window_end": "10:00:00", "duration_minutes": 60,
        "required_qualifications": ["rare_skill"],
    }).json()
    task = client.post(
        f"/plans/{plan['id']}/generate",
        headers={"X-Coordinator-ID": str(coord["id"])},
    ).json()["tasks"][0]

    resp = client.post(
        f"/tasks/{task['id']}/allocate",
        headers={"X-Coordinator-ID": str(coord["id"])},
    )
    assert resp.status_code == 409
    body = resp.json()["detail"]
    assert body["allocated"] is False
    codes = {v["code"] for v in body["violations"]}
    assert "qualification_missing_or_expired" in codes


def test_timeout_reassign_via_admin_endpoint(client, advance):
    unit, coord, _, w1, w2 = _setup(client)
    plan = _make_plan(client, coord, unit)
    task = client.post(
        f"/plans/{plan['id']}/generate",
        headers={"X-Coordinator-ID": str(coord["id"])},
    ).json()["tasks"][0]
    first = client.post(
        f"/tasks/{task['id']}/allocate",
        headers={"X-Coordinator-ID": str(coord["id"])},
    ).json()["assignment"]
    assert first["worker_id"] == w1["id"]

    advance(timedelta(minutes=8, seconds=1))
    reaped = client.post(
        "/admin/reap-expired", headers={"X-Coordinator-ID": str(coord["id"])}
    ).json()
    assert reaped["expired"] == 1
    assert reaped["reallocated"] == 1
    new_offer = reaped["results"][0]["assignment"]
    assert new_offer["worker_id"] == w2["id"]


def test_events_history_available(client):
    unit, coord, _, w1, _ = _setup(client)
    plan = _make_plan(client, coord, unit)
    task = client.post(
        f"/plans/{plan['id']}/generate",
        headers={"X-Coordinator-ID": str(coord["id"])},
    ).json()["tasks"][0]
    client.post(
        f"/tasks/{task['id']}/allocate",
        headers={"X-Coordinator-ID": str(coord["id"])},
    )
    events = client.get(
        f"/tasks/{task['id']}/events",
        headers={"X-Coordinator-ID": str(coord["id"])},
    ).json()
    assert any(e["action"] == "invitation_created" for e in events)


def test_manual_assign_blocked_then_forced_consistency(client):
    """Manual assignment that violates constraints returns 409 and leaves the
    task pending; a valid manual assignment afterwards succeeds."""
    unit, coord, outsider, w1, _ = _setup(client)
    plan = client.post(
        "/plans", headers={"X-Coordinator-ID": str(coord["id"])}, json={
            "care_recipient_id": "r", "unit_id": unit["id"],
            "recurrence": "daily", "window_start": "08:00:00",
            "window_end": "10:00:00", "duration_minutes": 60,
            "required_qualifications": ["nonexistent_skill"],
        }).json()
    task = client.post(
        f"/plans/{plan['id']}/generate",
        headers={"X-Coordinator-ID": str(coord["id"])},
    ).json()["tasks"][0]

    resp = client.post(
        f"/tasks/{task['id']}/manual-assign",
        headers={"X-Coordinator-ID": str(coord["id"])},
        json={"worker_id": w1["id"], "reason": "test override"},
    )
    assert resp.status_code == 409
    assert "qualification_missing_or_expired" in str(resp.json()["detail"])

    stored = client.get(f"/tasks/{task['id']}").json()
    assert stored["status"] == TaskStatus.pending.value
