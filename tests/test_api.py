"""End-to-end API tests via TestClient: clock, plans, allocation, accept,
reaping, coordinator authorization, manual adjustments and audit history."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import (
    Assignment,
    AuditLog,
    Coordinator,
    Invitation,
    Qualification,
    Task,
    Unit,
    Worker,
    WorkerQualification,
)
from tests.conftest import (
    CNA,
    LIFT,
    coord_headers,
    grant_qualification,
    make_coordinator,
    make_qualification,
    make_unit,
    make_worker,
    worker_headers,
)

MON = datetime(2026, 9, 21, 0, 0, tzinfo=timezone.utc)


def _directory(db: Session):
    unit = make_unit(db)
    admin = make_coordinator(db, admin=True)
    scoped = make_coordinator(db, unit=unit)
    other_unit = make_unit(db, "Other")
    locked_out = make_coordinator(db, unit=other_unit)
    make_qualification(db, CNA)
    qual = db.scalar(select(Qualification).where(Qualification.code == CNA))
    w1 = make_worker(db, unit)
    w2 = make_worker(db, unit)
    grant_qualification(db, w1, qual)
    grant_qualification(db, w2, qual)
    db.flush()
    return unit, admin, scoped, locked_out, w1, w2


def _plan_body(unit_id: int):
    return {
        "external_id": "API-1",
        "client_name": "Mrs. Li",
        "unit_id": unit_id,
        "timezone": "UTC",
        "templates": [{
            "code": "VISIT",
            "name": "Daily visit",
            "window_start_minute": 10 * 60,
            "window_end_minute": 11 * 60,
            "duration_minutes": 60,
            "weekday_mask": [],
            "qualification_codes": [CNA],
            "prerequisite_codes": [],
        }],
    }


def test_health_and_clock_control(client: TestClient):
    r = client.get("/health")
    assert r.status_code == 200
    r = client.post("/internal/clock", json={"freeze_at": MON.isoformat()})
    assert r.status_code == 200
    assert r.json()["now"].startswith("2026-09-21T00:00:00")
    r = client.post("/internal/clock", json={"advance_seconds": 480})
    assert r.json()["now"].startswith("2026-09-21T00:08:00")


def test_full_flow_create_allocate_accept_reap(client: TestClient, db: Session):
    unit, admin, scoped, _locked, w1, w2 = _directory(db)
    client.post("/internal/clock", json={"freeze_at": MON.isoformat()})

    # Scoped coordinator creates the plan in their authorized unit.
    r = client.post("/plans", json=_plan_body(unit.id), headers=coord_headers(scoped))
    assert r.status_code == 201, r.text
    plan = r.json()
    assert plan["generated_task_count"] == 14

    r = client.get("/tasks", params={"plan_id": plan["id"], "limit": 1})
    task = r.json()[0]

    # Allocate: lower-id feasible worker (w1) wins.
    r = client.post(f"/tasks/{task['id']}/allocate", headers=coord_headers(scoped))
    assert r.status_code == 200, r.text
    alloc = r.json()
    assert alloc["status"] == "invited" and alloc["worker_id"] == w1.id
    invitation_id = alloc["invitation_id"]

    # w2 cannot accept w1's invitation.
    r = client.post(f"/worker/invitations/{invitation_id}/accept", headers=worker_headers(w2))
    assert r.status_code == 403

    # w1 accepts.
    r = client.post(f"/worker/invitations/{invitation_id}/accept", headers=worker_headers(w1))
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "assigned"
    assert r.json()["assigned_worker_id"] == w1.id

    # Repeat accept is idempotent, no double assignment.
    r = client.post(f"/worker/invitations/{invitation_id}/accept", headers=worker_headers(w1))
    assert r.status_code == 200
    assert db.scalar(select(Assignment).where(Assignment.task_id == task["id"])) is not None
    assert len(db.scalars(select(Assignment)).all()) == 1

    # Repeated allocate on an assigned task is a no-op.
    r = client.post(f"/tasks/{task['id']}/allocate", headers=coord_headers(scoped))
    assert r.json()["status"] == "unchanged"


def test_timeout_reassigns_via_clock_and_reaper(client: TestClient, db: Session):
    unit, admin, scoped, _locked, w1, w2 = _directory(db)
    client.post("/internal/clock", json={"freeze_at": MON.isoformat()})
    plan = client.post("/plans", json=_plan_body(unit.id), headers=coord_headers(scoped)).json()
    task = client.get("/tasks", params={"plan_id": plan["id"], "limit": 1}).json()[0]

    first = client.post(f"/tasks/{task['id']}/allocate", headers=coord_headers(scoped)).json()
    assert first["worker_id"] == w1.id

    # 9 minutes pass; reap -> w1 expired, w2 invited.
    client.post("/internal/clock", json={"advance_seconds": 9 * 60})
    r = client.post("/internal/reap-expired")
    assert r.status_code == 200
    assert task["id"] in r.json()["reallocated_task_ids"]

    detail = client.get(f"/tasks/{task['id']}").json()
    assert detail["status"] == "invited" and detail["assigned_worker_id"] is None
    assert detail["active_invitation_id"] != first["invitation_id"]
    invs = client.get(f"/tasks/{task['id']}/invitations").json()
    assert {(i["worker_id"], i["status"]) for i in invs} == {
        (w1.id, "expired"), (w2.id, "pending"),
    }

    # w1's late accept is rejected.
    r = client.post(f"/worker/invitations/{first['invitation_id']}/accept",
                    headers=worker_headers(w1))
    assert r.status_code == 409 and r.json()["detail"]["error"] == "invitation_expired"
    assert len(db.scalars(select(Assignment)).all()) == 0

    # w2 accepts the new invitation.
    second = detail["active_invitation_id"]
    r = client.post(f"/worker/invitations/{second}/accept", headers=worker_headers(w2))
    assert r.status_code == 200 and r.json()["assigned_worker_id"] == w2.id


def test_coordinator_unit_authorization_enforced(client: TestClient, db: Session):
    unit, admin, scoped, locked_out, w1, w2 = _directory(db)
    client.post("/internal/clock", json={"freeze_at": MON.isoformat()})

    # Locked-out coordinator cannot create a plan in unit.
    r = client.post("/plans", json=_plan_body(unit.id), headers=coord_headers(locked_out))
    assert r.status_code == 403

    plan = client.post("/plans", json=_plan_body(unit.id), headers=coord_headers(admin)).json()
    task = client.get("/tasks", params={"plan_id": plan["id"], "limit": 1}).json()[0]

    # Neither allocation nor manual ops nor candidate preview are allowed.
    assert client.post(f"/tasks/{task['id']}/allocate",
                       headers=coord_headers(locked_out)).status_code == 403
    assert client.get(f"/tasks/{task['id']}/candidates",
                      headers=coord_headers(locked_out)).status_code == 403
    assert client.post(f"/tasks/{task['id']}/manual-assign",
                       json={"worker_id": w1.id, "reason": "x"},
                       headers=coord_headers(locked_out)).status_code == 403

    # Missing identity header is 401.
    assert client.post(f"/tasks/{task['id']}/allocate").status_code == 401


def test_manual_assignment_constraint_checked_and_audited(client: TestClient, db: Session):
    unit, admin, scoped, _locked, w1, w2 = _directory(db)
    client.post("/internal/clock", json={"freeze_at": MON.isoformat()})
    plan = client.post("/plans", json=_plan_body(unit.id), headers=coord_headers(scoped)).json()
    tasks = client.get("/tasks", params={"plan_id": plan["id"], "limit": 3}).json()

    # Manually assign w1 to task 1.
    r = client.post(f"/tasks/{tasks[0]['id']}/manual-assign",
                    json={"worker_id": w1.id, "reason": "client prefers Anna"},
                    headers=coord_headers(scoped))
    assert r.status_code == 200 and r.json()["assigned_worker_id"] == w1.id

    # Assigning w1 to a task with <10h rest after the first must be rejected.
    # Move day-2 visit to only 9h after day-1's visit ends (10:00-11:00 day1
    # -> 20:00-21:00 day1 == 9h rest).
    t1_end = datetime.fromisoformat(tasks[0]["scheduled_end"])
    tight_start = t1_end + timedelta(hours=9)
    rr = client.post(f"/tasks/{tasks[1]['id']}/manual-reschedule",
                     json={"scheduled_start": tight_start.isoformat(),
                           "scheduled_end": (tight_start + timedelta(hours=1)).isoformat(),
                           "reason": "test setup"},
                     headers=coord_headers(scoped))
    assert rr.status_code == 200, rr.text
    r = client.post(f"/tasks/{tasks[1]['id']}/manual-assign",
                    json={"worker_id": w1.id, "reason": "try tight rest"},
                    headers=coord_headers(scoped))
    assert r.status_code == 422
    codes = {v["constraint"] for v in r.json()["detail"]["violations"]}
    assert "insufficient_rest" in codes

    # Manual reschedule to an early slot. Release the assignment first so the
    # move itself is not blocked by that day's generated visit; the audit of
    # the move is what this assertion checks.
    task0 = client.get(f"/tasks/{tasks[0]['id']}").json()
    start_dt = datetime.fromisoformat(task0["scheduled_start"]) + timedelta(days=12, hours=-4)
    client.post(f"/tasks/{tasks[0]['id']}/manual-unassign",
                json={"reason": "moving slot"}, headers=coord_headers(scoped))
    r = client.post(f"/tasks/{tasks[0]['id']}/manual-reschedule",
                    json={"scheduled_start": start_dt.isoformat(),
                          "scheduled_end": (start_dt + timedelta(hours=1)).isoformat(),
                          "reason": "family request"},
                    headers=coord_headers(scoped))
    assert r.status_code == 200, r.text

    history = client.get(f"/tasks/{tasks[0]['id']}/history").json()
    actions = [h["action"] for h in history]
    assert "assignment.manual_created" in actions
    assert "task.manual_rescheduled" in actions
    reasons = {h["action"]: h["reason"] for h in history}
    assert reasons["assignment.manual_created"] == "client prefers Anna"
    assert reasons["task.manual_rescheduled"] == "family request"


def test_manual_unassign_releases_capacity(client: TestClient, db: Session):
    unit, admin, scoped, _locked, w1, w2 = _directory(db)
    client.post("/internal/clock", json={"freeze_at": MON.isoformat()})
    plan = client.post("/plans", json=_plan_body(unit.id), headers=coord_headers(scoped)).json()
    task = client.get("/tasks", params={"plan_id": plan["id"], "limit": 1}).json()[0]
    client.post(f"/tasks/{task['id']}/manual-assign",
                json={"worker_id": w1.id, "reason": "r"}, headers=coord_headers(scoped))
    assert db.scalar(select(Assignment).where(Assignment.task_id == task["id"])) is not None

    r = client.post(f"/tasks/{task['id']}/manual-unassign",
                    json={"reason": "worker called in sick"}, headers=coord_headers(scoped))
    assert r.status_code == 200 and r.json()["status"] == "planned"
    assert db.scalar(select(Assignment).where(Assignment.task_id == task["id"])) is None


def test_weekly_hours_endpoint_splits_overnight(client: TestClient, db: Session):
    unit, admin, scoped, _locked, w1, w2 = _directory(db)
    client.post("/internal/clock", json={"freeze_at": MON.isoformat()})
    plan = client.post("/plans", json=_plan_body(unit.id), headers=coord_headers(scoped)).json()
    task = client.get("/tasks", params={"plan_id": plan["id"], "limit": 1}).json()[0]
    # Make this task cross the week boundary via manual reschedule
    # (Sunday 23:00 -> Monday 02:00 = 3h: 1h in week 1, 2h in week 2).
    sunday_evening = datetime(2026, 9, 27, 23, 0, tzinfo=timezone.utc)
    client.post(f"/tasks/{task['id']}/manual-reschedule",
                json={"scheduled_start": sunday_evening.isoformat(),
                      "scheduled_end": (sunday_evening + timedelta(hours=3)).isoformat(),
                      "reason": "overnight"}, headers=coord_headers(scoped))
    client.post(f"/tasks/{task['id']}/manual-assign",
                json={"worker_id": w1.id, "reason": "r"}, headers=coord_headers(scoped))

    r = client.get(f"/workers/{w1.id}/weekly-hours", headers=coord_headers(scoped))
    weeks = r.json()["weeks"]
    assert weeks["2026-09-21T00:00:00+00:00"] == 1.0
    assert weeks["2026-09-28T00:00:00+00:00"] == 2.0


def test_idempotent_regeneration_via_api(client: TestClient, db: Session):
    unit, admin, scoped, _locked, w1, w2 = _directory(db)
    client.post("/internal/clock", json={"freeze_at": MON.isoformat()})
    plan = client.post("/plans", json=_plan_body(unit.id), headers=coord_headers(scoped)).json()
    r = client.post(f"/plans/{plan['external_id']}/generate", headers=coord_headers(scoped))
    assert r.status_code == 200 and r.json()["generated_task_count"] == 14
    r = client.post(f"/plans/{plan['external_id']}/generate", headers=coord_headers(scoped))
    assert r.json()["generated_task_count"] == 14


def test_candidates_endpoint_reports_constraints(client: TestClient, db: Session):
    unit, admin, scoped, _locked, w1, w2 = _directory(db)
    client.post("/internal/clock", json={"freeze_at": MON.isoformat()})
    client.post("/directory/qualifications", json={"code": LIFT, "name": "Lift"})
    body = _plan_body(unit.id)
    body["templates"][0]["qualification_codes"] = [LIFT]  # nobody holds LIFT
    plan = client.post("/plans", json=body, headers=coord_headers(scoped)).json()
    assert "id" in plan, plan
    task = client.get("/tasks", params={"plan_id": plan["id"], "limit": 1}).json()[0]
    r = client.get(f"/tasks/{task['id']}/candidates", headers=coord_headers(scoped))
    assert r.status_code == 200
    candidates = {c["worker_id"]: c for c in r.json()["candidates"]}
    for c in candidates.values():
        assert c["feasible"] is False
        assert any(v["constraint"] == "qualification_not_covered" for v in c["violations"])

    # Allocation refuses rather than force-scheduling.
    r = client.post(f"/tasks/{task['id']}/allocate", headers=coord_headers(scoped))
    assert r.json()["status"] == "unassigned"
    assert client.get(f"/tasks/{task['id']}").json()["status"] == "unassigned"


def test_worker_cannot_start_task_assigned_to_another(client: TestClient, db: Session):
    unit, admin, scoped, _locked, w1, w2 = _directory(db)
    client.post("/internal/clock", json={"freeze_at": MON.isoformat()})
    plan = client.post("/plans", json=_plan_body(unit.id), headers=coord_headers(scoped)).json()
    task = client.get("/tasks", params={"plan_id": plan["id"], "limit": 1}).json()[0]
    client.post(f"/tasks/{task['id']}/manual-assign",
                json={"worker_id": w1.id, "reason": "r"}, headers=coord_headers(scoped))

    assert client.post(f"/worker/tasks/{task['id']}/start",
                       headers=worker_headers(w2)).status_code == 403
    r = client.post(f"/worker/tasks/{task['id']}/start", headers=worker_headers(w1))
    assert r.status_code == 200 and r.json()["status"] == "in_progress"
    assert client.post(f"/worker/tasks/{task['id']}/complete",
                       headers=worker_headers(w2)).status_code == 403
    r = client.post(f"/worker/tasks/{task['id']}/complete", headers=worker_headers(w1))
    assert r.status_code == 200 and r.json()["status"] == "completed"


def test_scoped_coordinator_weekly_hours_requires_unit_grant(client: TestClient, db: Session):
    unit, admin, scoped, locked_out, w1, w2 = _directory(db)
    assert client.get(f"/workers/{w1.id}/weekly-hours",
                      headers=coord_headers(scoped)).status_code == 200
    assert client.get(f"/workers/{w1.id}/weekly-hours",
                      headers=coord_headers(locked_out)).status_code == 403


def test_audit_scoped_to_authorized_units(client: TestClient, db: Session):
    unit, admin, scoped, locked_out, w1, w2 = _directory(db)
    client.post("/internal/clock", json={"freeze_at": MON.isoformat()})
    plan = client.post("/plans", json=_plan_body(unit.id), headers=coord_headers(scoped)).json()
    task = client.get("/tasks", params={"plan_id": plan["id"], "limit": 1}).json()[0]
    client.post(f"/tasks/{task['id']}/allocate", headers=coord_headers(scoped))

    scoped_rows = client.get("/audit", headers=coord_headers(scoped)).json()
    locked_rows = client.get("/audit", headers=coord_headers(locked_out)).json()
    assert any(row["task_id"] == task["id"] for row in scoped_rows)
    assert locked_rows == []


def test_reschedule_cancels_pending_invitation_and_reopens_task(client: TestClient, db: Session):
    unit, admin, scoped, _locked, w1, w2 = _directory(db)
    client.post("/internal/clock", json={"freeze_at": MON.isoformat()})
    plan = client.post("/plans", json=_plan_body(unit.id), headers=coord_headers(scoped)).json()
    task = client.get("/tasks", params={"plan_id": plan["id"], "limit": 1}).json()[0]
    alloc = client.post(f"/tasks/{task['id']}/allocate",
                        headers=coord_headers(scoped)).json()
    assert alloc["status"] == "invited"

    new_start = datetime.fromisoformat(task["scheduled_start"]) + timedelta(hours=2)
    r = client.post(f"/tasks/{task['id']}/manual-reschedule",
                    json={"scheduled_start": new_start.isoformat(),
                          "scheduled_end": (new_start + timedelta(hours=1)).isoformat(),
                          "reason": "facility changed"},
                    headers=coord_headers(scoped))
    assert r.status_code == 200
    assert r.json()["status"] == "planned"
    assert r.json()["active_invitation_id"] is None

    invs = client.get(f"/tasks/{task['id']}/invitations").json()
    assert invs[0]["status"] == "cancelled"

    # The old invitation can no longer be accepted.
    r = client.post(f"/worker/invitations/{alloc['invitation_id']}/accept",
                    headers=worker_headers(w1))
    assert r.status_code == 409

    # Reallocation issues a fresh invitation in a new generation.
    r = client.post(f"/tasks/{task['id']}/allocate", headers=coord_headers(scoped))
    assert r.json()["status"] == "invited" and r.json()["round"] == 2
