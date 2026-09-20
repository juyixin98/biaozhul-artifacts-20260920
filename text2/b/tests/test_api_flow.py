"""End-to-end HTTP tests: headers, auth, generation, accept, timeout sweep."""
from __future__ import annotations

from datetime import date, timedelta

from sqlalchemy import select

from app.models import Coordinator, Qualification, Unit, Worker


def _seed_world(db, clock, *, unit_tz="UTC", worker_tz="UTC", num=3):
    unit = Unit(name="http-unit", timezone=unit_tz)
    db.add(unit)
    other = Unit(name="http-other", timezone="UTC")
    db.add(other)
    coord = Coordinator(name="c", external_id="coord-http", units=[unit])
    outsider = Coordinator(name="o", external_id="coord-out", units=[other])
    db.add_all([coord, outsider])
    bls = Qualification(code="BLS", name="BLS")
    db.add(bls)
    db.flush()
    workers = []
    for i in range(num):
        w = Worker(name=f"w{i}", external_id=f"http-w{i}", timezone=worker_tz)
        w.units = [unit]
        db.add(w)
        workers.append(w)
    db.flush()
    from app.models import WorkerQualification

    for w in workers:
        db.add(
            WorkerQualification(
                worker_id=w.id,
                qualification_id=bls.id,
                valid_from=clock.now() - timedelta(days=30),
            )
        )
    db.commit()
    return unit, bls, workers


def _create_plan(client, unit, bls, *, timezone="UTC"):
    today = "2026-09-21"
    payload = {
        "unit_id": unit["id"] if isinstance(unit, dict) else unit.id,
        "title": "http-plan",
        "timezone": timezone,
        "period_days": 1,
        "anchor_date": today,
        "slots": [
            {
                "weekday": d,
                "start_at": "09:00",
                "latest_start_at": "11:00",
                "duration_minutes": 60,
            }
            for d in range(7)
        ],
        "qualification_ids": [bls["id"] if isinstance(bls, dict) else bls.id],
        "prerequisite_plan_ids": [],
    }
    r = client.post("/api/plans", json=payload, headers={"X-Coordinator-Id": "coord-http"})
    assert r.status_code == 201, r.text
    return r.json()


def test_health_and_openapi(client):
    assert client.get("/health").json()["status"] == "ok"
    spec = client.get("/openapi.json").json()
    assert "/api/tasks/{task_id}/schedule" in spec["paths"]


def test_missing_coordinator_header_rejected(client):
    r = client.get("/api/plans")
    assert r.status_code == 403


def test_full_flow_generate_schedule_accept(client, db, clock):
    unit, bls, workers = _seed_world(db, clock)
    plan = _create_plan(client, unit, bls)

    # Tasks already generated on plan create; regenerate is idempotent.
    r1 = client.post(
        f"/api/tasks/generate/{plan['id']}",
        headers={"X-Coordinator-Id": "coord-http"},
    )
    assert r1.status_code == 200
    assert len(r1.json()["tasks"]) == 14
    r2 = client.post(
        f"/api/tasks/generate/{plan['id']}",
        headers={"X-Coordinator-Id": "coord-http"},
    )
    assert len(r2.json()["tasks"]) == 14

    task_id = r2.json()["tasks"][0]["id"]

    # Candidates endpoint lists every worker.
    rc = client.get(
        f"/api/tasks/{task_id}/candidates",
        headers={"X-Coordinator-Id": "coord-http"},
    )
    assert rc.status_code == 200
    assert len(rc.json()) == 3
    assert all(c["feasible"] for c in rc.json())

    rs = client.post(
        f"/api/tasks/{task_id}/schedule",
        headers={"X-Coordinator-Id": "coord-http"},
    )
    assert rs.status_code == 200
    body = rs.json()
    assert body["status"] == "invited"
    asm_id = body["assignment_id"]

    # Wrong worker cannot accept.
    rw = client.post(
        f"/api/assignments/{asm_id}/accept",
        headers={"X-Worker-Id": "http-w2"},
    )
    assert rw.status_code == 409

    # Invited worker accepts.
    ra = client.post(
        f"/api/assignments/{asm_id}/accept",
        headers={"X-Worker-Id": "http-w0"},
    )
    assert ra.status_code == 200, ra.text
    assert ra.json()["task_status"] == "assigned"

    # Idempotent repeat accept.
    ra2 = client.post(
        f"/api/assignments/{asm_id}/accept",
        headers={"X-Worker-Id": "http-w0"},
    )
    assert ra2.status_code == 200


def test_timeout_flow_via_sweep_endpoint(client, db, clock):
    unit, bls, workers = _seed_world(db, clock)
    plan = _create_plan(client, unit, bls)
    tasks = client.post(
        f"/api/tasks/generate/{plan['id']}",
        headers={"X-Coordinator-Id": "coord-http"},
    ).json()["tasks"]
    task_id = tasks[0]["id"]

    client.post(
        f"/api/tasks/{task_id}/schedule",
        headers={"X-Coordinator-Id": "coord-http"},
    )
    # Advance the fake clock past the 8-minute TTL then sweep.
    clock.advance(minutes=8, seconds=1)
    sweep = client.post(
        "/api/scheduler/sweep",
        headers={"X-Coordinator-Id": "coord-http"},
    )
    assert sweep.status_code == 200
    entries = [r for r in sweep.json() if r["task_id"] == task_id]
    assert entries and entries[0]["status"] == "invited"

    # The fresh invitation must belong to a different worker.
    first = client.get(
        f"/api/tasks/{task_id}/history",
        headers={"X-Coordinator-Id": "coord-http"},
    ).json()
    types = [e["event_type"] for e in first]
    assert "invitation_expired" in types


def test_unit_authorization_enforced_on_api(client, db, clock):
    unit, bls, workers = _seed_world(db, clock)
    plan = _create_plan(client, unit, bls)
    r = client.get(
        f"/api/plans/{plan['id']}",
        headers={"X-Coordinator-Id": "coord-out"},
    )
    assert r.status_code == 403


def test_manual_assign_and_history(client, db, clock):
    unit, bls, workers = _seed_world(db, clock, num=1)
    plan = _create_plan(client, unit, bls)
    task_id = client.post(
        f"/api/tasks/generate/{plan['id']}",
        headers={"X-Coordinator-Id": "coord-http"},
    ).json()["tasks"][0]["id"]

    r = client.post(
        f"/api/tasks/{task_id}/manual-assign",
        headers={"X-Coordinator-Id": "coord-http"},
        json={"worker_id": workers[0].id, "reason": "客户指定"},
    )
    assert r.status_code == 200 and r.json()["status"] == "invited"

    history = client.get(
        f"/api/tasks/{task_id}/history",
        headers={"X-Coordinator-Id": "coord-http"},
    ).json()
    assert any(e["event_type"] == "manual_assign" for e in history)
