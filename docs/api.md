# SIRCC API

Base URL: `http://localhost:8080`. All requests and responses are JSON.

## Authentication (header-based, no external IdP)

| Header       | Values                            |
|--------------|-----------------------------------|
| `X-User-ID`  | arbitrary user identifier         |
| `X-User-Role`| `analyst` \| `responder` \| `admin` |

Missing/invalid headers → `401 UNAUTHENTICATED`.

## Roles and case scope

- **admin** — assigns personnel to an incident (`POST /incidents/{id}/members`).
- **analyst** — assigned analysts complete triage and submit/correct evidence.
- **responder** — assigned responders advance containment → eradication →
  recovery → postmortem → closed, record postmortem fields, and manage action
  items (assigned analysts can also create/reschedule action items).

Operating on a case without the required role *and* assignment → `403 FORBIDDEN`.

## Incident lifecycle

```
detected → triaged → contained → eradicated → recovered → postmortem → closed
```

Exactly one successor per phase — skipping is rejected (`400 INVALID_TRANSITION`).

Gates:

- `detected → triaged` on a **P1** incident requires at least one assigned
  responder (`400 GATE_UNMET` otherwise).
- `postmortem → closed` requires a non-empty root cause, non-empty lessons
  learned, and ≥ 1 action item (owner + due date are mandatory fields).

## Endpoints

### Incidents

- `POST /incidents` — `{title, severity}` (`P1`–`P4`) → `201` incident.
- `GET /incidents` — list.
- `GET /incidents/{id}` — incident + `durations`
  (`time_to_contain_seconds`, `time_to_resolve_seconds`; `null` until the
  relevant phases have valid timestamps — never fabricated).

### Personnel

- `POST /incidents/{id}/members` (admin) — `{user_id, role}` (`analyst`|`responder`).
- `GET /incidents/{id}/members`.

### Transitions (optimistic versioning + idempotency)

`POST /incidents/{id}/transitions`

```json
{"to": "triaged", "expected_version": 1, "request_id": "client-uuid-1"}
```

- `expected_version` must equal the incident's current `version`, otherwise
  `409 VERSION_CONFLICT`.
- `request_id` makes the transition idempotent: a replay returns the original
  stored result with `"replayed": true`; reusing the id for a different
  incident → `409 REQUEST_ID_CONFLICT`.
- Status bump, phase-record close/open, audit event and the idempotency
  record commit in **one transaction** — no half-updates.

### Postmortem

- `PUT /incidents/{id}/postmortem` (assigned responder, postmortem phase) —
  `{root_cause, lessons_learned}`.

### Evidence (max 50 per incident, immutable)

- `POST /incidents/{id}/evidence` (assigned analyst) — `{content}` → `201`.
  The cap is enforced by an atomic conditional counter, so concurrent adds
  cannot exceed 50 (`409 EVIDENCE_LIMIT`).
- `POST /evidence/{evidenceId}/notes` (assigned analyst) — `{note}`.
  Submitted evidence cannot be overwritten; corrections are append-only
  linked notes.

### Action items & reminders

- `POST /incidents/{id}/action-items` — `{title, owner_id, due_at}` (RFC3339).
- `GET /incidents/{id}/action-items`.
- `POST /action-items/{itemId}/reschedule` — `{due_at}`; bumps `due_version`,
  invalidating any unsent reminder for the old schedule.
- `GET /incidents/{id}/reminders` — persisted reminders. A background worker
  (interval `REMINDER_INTERVAL`, default 5s) writes exactly one reminder per
  `(action_item, due_version)` once `due_at` passes. It is DB-driven, so a
  restart catches up on anything overdue without duplicating.

### Audit & export

- `GET /incidents/{id}/audit` — full audit trail.
- `GET /incidents/{id}/export` — incident, durations, phase records, evidence
  summary (excerpts + correction notes), action items, audit events.

### Misc

- `GET /healthz`.

## Error format

```json
{"error": {"code": "VERSION_CONFLICT", "message": "stale version: expected 1, current version is 2"}}
```

Codes: `VALIDATION`, `BAD_JSON`, `BAD_ID`, `UNAUTHENTICATED` (401),
`FORBIDDEN` (403), `NOT_FOUND` (404), `INVALID_TRANSITION`, `INVALID_PHASE`,
`GATE_UNMET`, `INCIDENT_CLOSED`, `VERSION_CONFLICT`, `REQUEST_ID_CONFLICT`,
`EVIDENCE_LIMIT` (409), `INTERNAL` (500).
