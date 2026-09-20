# SIRCC API

Security Incident Response Command Center — backend reference implementation.
All endpoints are JSON over HTTP and run entirely on PostgreSQL; there is no
connection to real security devices or external services.

## Conventions

| Header | Required for | Purpose |
|---|---|---|
| `X-User-Id` | every `/v1/*` route | Caller identity. Must match a row in `users`. |
| `X-Request-Id` | every state-changing request | Idempotency key. See below. |

- Timestamps are RFC3339 / UTC.
- Errors have one shape:
  ```json
  { "error": { "code": "forbidden", "message": "..." } }
  ```
- Pagination on list endpoints: `?limit=` (1–200, default 50) and `?offset=`.

### Idempotency and optimistic concurrency

Every mutating request requires a client-generated `X-Request-Id`.

- **First request** performs the action and stores the exact response.
- **Repeated request** with the same `X-Request-Id` returns the stored
  response/status verbatim and adds `Idempotent-Replay: true`. The business
  operation is never executed twice.
- A request id whose prior attempt crashed before completing yields `409
  duplicate_request`; send a fresh id.

Transitions and reschedules additionally carry an `expected_version`
(optimistic lock). A stale version is rejected with `409 version_conflict`;
the caller re-reads the current version and retries.

### Error codes

| HTTP | code | Meaning |
|---|---|---|
| 400 | `bad_request` | Malformed body / missing idempotency key |
| 401 | `unauthorized` | Missing/unknown `X-User-Id` |
| 403 | `forbidden` | Role not permitted, or not a member of the case |
| 404 | `not_found` | Resource missing |
| 409 | `version_conflict` | Stale `expected_version` |
| 409 | `invalid_transition` | Case already closed / illegal edge |
| 409 | `duplicate_request` | Request id in an unfinished state |
| 422 | `gate_failed` | A stage gate (P1 triage, postmortem, close) failed |
| 422 | `evidence_capacity` | 50-evidence per case limit reached |

## Roles and permissions

| Role | May |
|---|---|
| `analyst` | Create cases, run **triage**, author evidence & correction notes, write the **postmortem** |
| `responder` | Drive **containment → eradication → recovery**, manage action items, **close** |
| `admin` | Everything above plus **assign** analysts/responders; see all cases and the global audit trail |

Case scope: a non-admin may only touch incidents they belong to. The creator
is a member; assignment and action-item ownership also grant membership.

## Lifecycle

```
detected → triaged → contained → eradicated → recovered → postmortem → closed
```

Stages cannot be skipped; each transition moves only to the immediate next
stage. Stage row, stage timestamp, `stage_events` row, and `audit_events` row
are written in a single transaction (no half-updates).

**Gates**

- A **P1** incident cannot complete `detected → triaged` until an admin has
  assigned a responder.
- `recovered → postmortem` requires `root_cause` and `lessons_learned`
  (supplied on the postmortem transition; carried onto close).
- `postmortem → closed` requires the postmortem fields **and** at least one
  action item with an owner and due date, with **no open** items remaining.

## Endpoints

### Users
```
GET /v1/users
```

### Incidents
```
POST /v1/incidents                 { "title", "severity": "P1".."P4" }
GET  /v1/incidents                 ?scope=mine|all (all: admin) &severity=
GET  /v1/incidents/{id}
POST /v1/incidents/{id}/transition { "expected_version", "note",
                                     "root_cause"?, "lessons_learned"? }
POST /v1/incidents/{id}/assignments   (admin) { "user_id", "role": "analyst"|"responder" }
GET  /v1/incidents/{id}/events       stage timeline
GET  /v1/incidents/{id}/export       full case export (?download=1)
```

The transition endpoint always advances to the **next** stage; the role on the
request determines authorization.

### Evidence (max 50 text items per case)
```
POST /v1/incidents/{id}/evidence                 { "content" }   (analyst)
GET  /v1/incidents/{id}/evidence
POST /v1/incidents/{id}/evidence/{evidenceId}/notes { "content" }  (analyst)
```
Evidence is immutable — there is no update/delete. Corrections are appended as
linked notes. The 50-item cap is enforced under a row lock so concurrent
inserts cannot exceed it.

### Action items
```
POST /v1/incidents/{id}/action-items { "title", "owner_id", "due_at" }
GET  /v1/incidents/{id}/action-items
POST /v1/action-items/{itemId}/reschedule { "new_due_at", "expected_version" }
POST /v1/action-items/{itemId}/status     { "status": "open"|"done"|"cancelled" }
```

### Notifications & audit
```
GET /v1/notifications              caller's reminders (?scope=all for admin)
GET /v1/audit-events               admin global; others must pass ?incident_id=
```

## Reminders

When an open action item passes its `due_at`, the background scheduler
persists a notification. Reminders are durable and de-duplicated on
`(action_item_id, version)`:

- the same due schedule version produces exactly one notification, even across
  overlapping ticks or multiple instances;
- rescheduling bumps `version`, so an old schedule can never fire a stale
  reminder;
- scheduler state is only the database, so a restart immediately processes
  everything that came due while it was down (startup catch-up).

## Duration metrics (export)

Computed only from stages actually reached:

- `containment_seconds = contained_at − triaged_at`
- `resolution_seconds  = closed_at − triaged_at`

An unfinished case leaves these fields `null`; no end timestamp is ever
fabricated for an un-reached stage.
