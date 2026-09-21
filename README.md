# CareForce — Nursing-Care Task Scheduling Backend

FastAPI + SQLAlchemy 2.0 + PostgreSQL 16 + Alembic. The service owns exactly
three capabilities:

1. **Task generation** — recurring care-plan occurrences for a rolling 14-day
   horizon, anchored to the plan's service timezone.
2. **Constraint-based assignment** — qualifications, no overlap, 10h rest,
   44h weekly cap, ranked candidate selection.
3. **Timeout reassignment** — 8-minute invitation TTL, automatic re-invitation,
   concurrency-safe acceptance.

It deliberately contains **no payroll, volunteer management, or clinical /
medical decision logic**.

## Quick start (Docker)

```bash
docker compose up --build
```

This starts PostgreSQL 16, applies Alembic migrations automatically and serves
the API on http://localhost:8000.

* Interactive API docs (Swagger): http://localhost:8000/docs
* ReDoc: http://localhost:8000/redoc
* Health: http://localhost:8000/health

Load a demo dataset (unit, coordinators, qualified workers, a care plan):

```bash
docker compose --profile seed run --rm seed
```

### Local development (without Docker for the app)

```bash
createdb careforce careforce_test   # or: see scripts/seed for defaults
pip install -r requirements.txt
cp .env.example .env
alembic upgrade head
uvicorn app.main:app --reload
```

The default DB URL is
`postgresql+psycopg2://careforce:careforce@localhost:5432/careforce` and can
be overridden with `CAREFORCE_DATABASE_URL`.

## Business rules

### Task generation

* Each plan has a service **timezone**, recurring templates with a daily
  **time window** (`window_start_minute`/`window_end_minute`), a
  **duration**, required **qualification codes**, an optional ISO-weekday mask
  (1=Mon…7=Sun, empty = every day) and **prerequisite** template codes.
* Generation covers the next **14 service-local days** from "now". Occurrences
  that already ended are never created.
* The natural key `(plan revision, template, service-local date)` makes
  generation **idempotent**: regenerating the same plan, including concurrent
  requests, never creates duplicate work orders (a plan-row lock serializes
  generators; the unique constraint is the hard backstop).
* **Plan revisions only affect tasks that have not started.** Started /
  in-progress / completed occurrences stay on the old revision with their
  assignments; every other future occurrence is cancelled (releasing pending
  invitations and capacity) and regenerated from the new revision.

### Assignment constraints

Every route that puts a worker on a task — automatic allocation, invitation
acceptance, and coordinator manual assignment — enforces the same rules:

| Constraint | Rule |
|---|---|
| Qualifications | Every required qualification must be held and valid over the **entire** task interval (`valid_from <= start`, `valid_until >= end`; null `valid_until` = never expires) |
| No overlap | Half-open intervals may not intersect any load-bearing assignment |
| Rest | At least **10 hours** between the end of one shift and the start of the next (both directions) |
| Weekly hours | At most **44 hours per ISO week** (Monday 00:00 UTC). Overnight / cross-midnight shifts are **split at the week boundary** and count in each week they touch |
| Unit | Workers belong to one unit and can only serve that unit's tasks |
| Active | Inactive workers are never invited (but still appear in constraint reports) |

**Candidate ranking:** feasible workers are ordered by

1. most **remaining available time** in the tightest touched ISO week
   (44h minus existing weekly load),
2. then **least existing load** in the lookup window,
3. then **worker id ascending** — stable tie-breaking.

If nobody is feasible the task becomes `unassigned`; the response (and
`GET /tasks/{id}/candidates`) lists, per worker, the concrete unsatisfied
constraints. The system never force-schedules.

### Invitations, timeout and concurrency

* An allocation creates an **invitation** that expires **8 minutes** after
  creation (`CAREFORCE_INVITATION_TTL_MINUTES`).
* A background reaper (and `POST /internal/reap-expired`) expires due
  invitations and immediately invites the next-ranked feasible worker. Within
  one search generation a worker who expired/declined is not re-invited until
  every feasible candidate has had a turn; after that a new generation begins,
  so a lone feasible worker is simply re-invited each timeout.
* Only **one** `assignments` row may exist per task (unique constraint).
  Multiple workers accepting concurrently, or an accept racing the reaper,
  collapse onto the task row lock (`SELECT … FOR UPDATE` on task then
  invitations, in that fixed order): exactly one allocation survives, losing
  accepts get `409 already_assigned` / `409 invitation_expired` and consume no
  hours.
* Repeating the same accept request is **idempotent** — it returns the
  existing assignment and never double-counts time.

### Coordinator authorization & manual changes

* Coordinators pass `X-Coordinator-Id`. A coordinator may only schedule units
  listed in their grants; admins may schedule everything. Unauthorized unit
  access returns `403`.
* Manual assign / unassign / reschedule go through the **same constraint
  checks** as automatic allocation, require a free-text **reason**, and write
  an entry to the task's change history (`GET /tasks/{id}/history`,
  `GET /audit`).

## API overview

Identity headers: `X-Coordinator-Id: <id>` for coordinator calls,
`X-Worker-Id: <id>` for worker calls. Optionally set `CAREFORCE_API_KEY` and
send `X-API-Key` / `Authorization: Bearer …`.

### Directory

| Method | Path | Purpose |
|---|---|---|
| POST | `/directory/units` | create unit |
| GET | `/directory/units` | list units |
| POST | `/directory/coordinators` | create coordinator (`is_admin` optional) |
| GET | `/directory/coordinators` | list coordinators incl. granted units |
| POST | `/directory/coordinators/{id}/grants` | authorize a unit for a coordinator |
| POST | `/directory/workers` | create worker (`unit_id`, `active`) |
| GET | `/directory/workers?unit_id=` | list workers |
| POST | `/directory/qualifications` | create a qualification code |
| POST | `/directory/workers/{id}/qualifications` | grant a qualification with validity window |

### Plans & generation

| Method | Path | Purpose |
|---|---|---|
| POST | `/plans` | create plan + templates (generates 14 days immediately) |
| GET | `/plans` | list plans with generated-task counts |
| GET | `/plans/{external_id}` | fetch active revision |
| POST | `/plans/{external_id}/revisions` | revise: cancels not-started future tasks, regenerates |
| POST | `/plans/{external_id}/generate` | idempotently regenerate the rolling horizon |

Plan body example:

```json
{
  "external_id": "PLAN-42",
  "client_name": "Mrs. Zhang",
  "unit_id": 1,
  "timezone": "Asia/Shanghai",
  "templates": [
    {
      "code": "MORNING_CARE",
      "name": "Morning personal care",
      "window_start_minute": 480,
      "window_end_minute": 540,
      "duration_minutes": 60,
      "weekday_mask": [],
      "qualification_codes": ["CNA"],
      "prerequisite_codes": []
    }
  ]
}
```

All minutes are wall-clock minutes in the plan timezone (480 = 08:00). Task
intervals are anchored at the window start; timestamps in responses are UTC.

### Tasks, allocation, manual control

| Method | Path | Purpose |
|---|---|---|
| GET | `/tasks?status=&unit_id=&plan_id=` | list/filter tasks |
| GET | `/tasks/{id}` | task detail incl. assignee & live invitation |
| GET | `/tasks/{id}/candidates` | dry-run ranking + every worker's violations |
| GET | `/tasks/{id}/invitations` | all invitation rounds |
| GET | `/tasks/{id}/history` | audit / change history for the task |
| POST | `/tasks/{id}/allocate` | invite the best feasible candidate |
| POST | `/tasks/allocate-due` | reap expirations + allocate all open tasks |
| POST | `/tasks/{id}/manual-assign` | coordinator assignment `{worker_id, reason}` |
| POST | `/tasks/{id}/manual-unassign` | release assignment `{reason}` |
| POST | `/tasks/{id}/manual-reschedule` | move task `{scheduled_start, scheduled_end, reason}` |
| GET | `/workers/{id}/weekly-hours` | hours per ISO week (overnight split shown) |
| GET | `/audit?task_id=` | global audit feed |

### Worker

| Method | Path | Purpose |
|---|---|---|
| POST | `/worker/invitations/{id}/accept` | accept (idempotent; re-checked under lock) |
| POST | `/worker/invitations/{id}/decline` | decline (immediately invites next candidate) |
| POST | `/worker/tasks/{id}/start` | mark service in progress |
| POST | `/worker/tasks/{id}/complete` | mark service completed |

### Internal (test/ops)

| Method | Path | Purpose |
|---|---|---|
| GET | `/internal/clock` | current "now" |
| POST | `/internal/clock` | `{freeze_at}` / `{advance_seconds}` / `{reset}` controllable clock |
| POST | `/internal/reap-expired` | run the expiry/reallocation sweep on demand |

Disable the clock API in production with
`CAREFORCE_ENABLE_CLOCK_API=false`.

### Error shape

Constraint failures use HTTP 422 (or 409 for state conflicts) with a structured
body, e.g.:

```json
{
  "detail": {
    "error": "constraint_violation",
    "message": "candidate does not satisfy scheduling constraints",
    "violations": [
      {
        "constraint": "weekly_cap_exceeded",
        "message": "week of 2026-09-21 would reach 45.00h > 44h",
        "worker_id": 3,
        "detail": {"week_start": "2026-09-21T00:00:00+00:00", "used_minutes": 2700, "cap_minutes": 2640}
      }
    ]
  }
}
```

Constraint codes: `qualification_not_covered`, `time_overlap`,
`insufficient_rest`, `weekly_cap_exceeded`, `worker_inactive`,
`worker_outside_unit`, `task_not_open`, plus `invitation_expired` /
`already_assigned` on accepts.

## Testing

Tests run against PostgreSQL (`careforce_test`) using a **controllable
clock** (`app.clock.Clock`), so timeout behaviour is fully deterministic
without real sleeps.

```bash
# default: postgresql+psycopg2://careforce:careforce@localhost:5432/careforce_test
pytest
```

Coverage highlights (39 tests):

* **cross-week hours** — overnight shifts split at the ISO-week boundary and
  the 44h cap applied per week (`tests/test_timeutils.py`,
  `tests/test_constraints.py::test_overnight_shift_hours_split_across_weeks_for_cap`);
* **qualification expiry** — must cover the whole interval; an expiring cert is
  reported with `expires_before_end`;
* **concurrent accepts** — two real threads accepting different invitations
  for one task leave exactly one assignment;
* **expiry vs accept race** — reaper thread and accept thread simultaneously
  never produce two allocations;
* **timeout reassignment** — clock-driven 8-minute expiry moves the invitation
  to the next worker and the late accept is rejected;
* **repeated generation** — same-plan concurrent generators produce exactly 14
  occurrences;
* idempotent repeat accept, coordinator unit authorization, manual changes +
  audit, and the full HTTP flow end-to-end.

## Migrations

```bash
alembic revision --autogenerate -m "..."   # after model changes
alembic upgrade head
alembic downgrade -1
```

`alembic/env.py` reads `CAREFORCE_DATABASE_URL`, so the same migrations run in
Docker (entrypoint applies them automatically), locally, and against the test
database.

## Configuration

All settings are prefixed `CAREFORCE_`:

| Variable | Default | Meaning |
|---|---|---|
| `DATABASE_URL` | local postgres | SQLAlchemy/psycopg2 URL |
| `INVITATION_TTL_MINUTES` | 8 | invitation lifetime |
| `REST_BETWEEN_SHIFTS_HOURS` | 10 | minimum inter-shift rest |
| `WEEKLY_HOUR_CAP` | 44 | weekly hours cap |
| `GENERATION_HORIZON_DAYS` | 14 | rolling generation window |
| `REAPER_INTERVAL_SECONDS` | 30 | background sweep tick (0 disables) |
| `ENABLE_CLOCK_API` | true | expose `/internal/clock` |
| `API_KEY` | empty | when set, require X-API-Key/Bearer |

## Project layout

```
app/
  main.py                 # FastAPI app + background reaper
  clock.py                # controllable clock
  config.py  db.py  models.py  schemas.py  deps.py
  api/                    # directory, plans, tasks, worker, internal routers
  services/
    generation.py         # 14-day generation, idempotency, plan revisions
    constraints.py        # qualification/overlap/rest/weekly-cap + ranking
    allocation.py         # allocate, accept, decline, reap, manual ops
    allocation_candidates.py
    timeutils.py          # ISO-week splitting / overlap / tz anchoring
    audit.py
alembic/                  # migrations
scripts/seed.py           # demo dataset
tests/                    # 35 deterministic tests
```
