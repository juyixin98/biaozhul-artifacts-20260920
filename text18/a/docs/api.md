# SIRCC API Reference

Base URL: `http://localhost:8080`
All `/api/v1/**` endpoints require an API key, sent as either:

```
X-API-Key: sircc_demo_analyst_0001
# or
Authorization: Bearer sircc_demo_analyst_0001
```

## Conventions

- **Idempotency**: send `Idempotency-Key: <uuid>` (or `X-Request-ID`) on any
  mutating request. A repeated key with the same method + path returns the
  original response with the header `Idempotent-Replay: true` and performs no
  new work. Reusing a key for a different request returns `409
  idempotency_key_reuse`.
- **Optimistic concurrency**: transitions send `expected_version`. A mismatch
  returns `409 stale_version`; re-read the case and retry.
- **Errors**: JSON body `{"error":"<code>","message":"..."}`.

| Status | Codes                                                        |
|--------|--------------------------------------------------------------|
| 400    | `validation_error`                                           |
| 401    | `unauthorized`                                               |
| 403    | `forbidden` (wrong role or not a case member)               |
| 404    | `not_found`                                                   |
| 409    | `stale_version`, `invalid_transition`, `idempotency_key_reuse` |
| 422    | `precondition_failed` (`triage gate`, `closure gate`, evidence cap) |

## Lifecycle

```
POST /api/v1/incidents                                  detected
POST /api/v1/incidents/{id}/transitions  action=triage    → triaged
                                          action=contain   → contained
                                          action=eradicate → eradicated
                                          action=recover   → recovered
                                          action=review    → reviewed
                                          action=close     → closed
```

Allowed actor roles (non-admins also need case membership):

| action     | role      | extra gate                                                        |
|------------|-----------|-------------------------------------------------------------------|
| triage     | analyst   | P1 needs an assigned responder                                     |
| contain    | responder |                                                                   |
| eradicate  | responder |                                                                   |
| recover    | responder |                                                                   |
| review     | analyst   |                                                                   |
| close      | admin     | root cause + lessons learned + ≥1 effective action item           |

---

## Users

### `GET /api/v1/users`
Directory of active users (any authenticated caller).

---

## Incidents

### `POST /api/v1/incidents`
Creates a case at `detected`; the non-admin creator becomes a case member.

```json
{ "title": "Suspicious login spike", "description": "details...", "severity": "P1" }
```
`severity` ∈ `P1|P2|P3|P4`. Returns `201` with the incident, initial phase and
metrics.

### `GET /api/v1/incidents?status=&limit=&offset=`
Lists cases. Admins see all; other roles only cases they belong to.

### `GET /api/v1/incidents/{id}`
Full case: phases (ordered, with entry times), members, metrics, evidence,
action items, reminders and audit events. `403` for non-members.

### `POST /api/v1/incidents/{id}/transitions`
```json
{
  "action": "close",
  "expected_version": 6,
  "root_cause": "unpatched service exposed to the internet",
  "lessons_learned": "shorten patch SLA; add detection for this vector"
}
```
`root_cause` / `lessons_learned` are only required (and stored) on `close`.
Returns the updated case. Stale `expected_version` → `409`; gate failure →
`422`; skipped phase → `409 invalid_transition`.

---

## Membership (admin only)

### `PUT /api/v1/incidents/{id}/members/{userID}`
```json
{ "case_role": "responder" }
```
`case_role` ∈ `analyst|responder|admin` and must match the target user's
global role (except `admin`).

### `GET /api/v1/incidents/{id}/members`
Lists assigned people (case members and admins).

---

## Evidence

### `POST /api/v1/incidents/{id}/evidence`
```json
{ "content": "free text, 1..100000 chars" }
```
Analyst case members (and admins) only. `201` on success; the 51st item on a
case returns `422 precondition_failed` (`evidence limit reached...`). Supports
`Idempotency-Key`.

### `GET /api/v1/incidents/{id}/evidence`
Returns all items with attached correction notes.

### `POST /api/v1/incidents/{id}/evidence/{evidenceID}/notes`
Append a correction note. The original evidence body is never changed.
```json
{ "note": "timestamp was off by 5 minutes" }
```
A `{evidenceID}` belonging to a different case returns `404`.

---

## Action items

### `POST /api/v1/incidents/{id}/action-items`
```json
{
  "description": "Patch vulnerable host",
  "owner_user_id": "11111111-1111-1111-1111-111111111301",
  "due_at": "2026-10-01T12:00:00Z"
}
```
Any case member may create; owner must be an existing user. Returns the item
with `due_version: 1`.

### `GET /api/v1/incidents/{id}/action-items`
Returns `{ "action_items": [...], "reminders": [...] }`.

### `POST /api/v1/incidents/{id}/action-items/{itemID}/reschedule`
```json
{ "due_at": "2026-10-05T09:00:00Z" }
```
Atomically bumps `due_version`. Only the current schedule can be reminded, so
an old deadline can never produce a stale reminder.

### `POST /api/v1/incidents/{id}/action-items/{itemID}/status`
```json
{ "status": "done" }
```
`status` ∈ `done|canceled`. Canceled items are not reminded and do not satisfy
the closure gate.

---

## Reminders

Reminders are produced by the server scheduler (no endpoint creates them
directly). When an open item's `due_at` passes, the sweep inserts one durable
row per `(action_item_id, due_version)` and writes an audit event
(`action_item.due_reminded`). Repeated sweeps insert nothing; after a restart
the first sweep catches up on deadlines that passed while the service was
down. Reminders appear in `GET .../action-items` and in the export.

---

## Export

### `GET /api/v1/incidents/{id}/export`
Case report built from committed data:

- `phases[]` — every entered phase, actor, `entered_at`, and
  `duration_seconds` (`null` for the current, still-open phase).
- `metrics` — `time_to_containment_seconds`,
  `containment_phase_seconds`, `time_to_resolution_seconds`,
  `time_to_closure_seconds`, and per-phase durations. Null until the ending
  phase exists.
- `evidence_summary[]` — sequence number, length, 200-rune preview and
  correction-note count (full bodies are not duplicated).
- `action_items[]` and `reminders[]`.

---

## Example: complete P1 flow

```bash
API=:8080
AN='X-API-Key: sircc_demo_analyst_0001'
AD='X-API-Key: sircc_demo_admin_0001'
RP='X-API-Key: sircc_demo_responder_0001'

ID=$(curl -s -H "$AN" -H 'Content-Type: application/json' \
  -d '{"title":"P1 outage","severity":"P1"}' localhost$API/api/v1/incidents \
  | jq -r .id)

# 422: no responder yet
curl -s -H "$AN" -H 'Content-Type: application/json' \
  -d '{"action":"triage","expected_version":1}' \
  localhost$API/api/v1/incidents/$ID/transitions

curl -s -X PUT -H "$AD" -H 'Content-Type: application/json' \
  -d '{"case_role":"responder"}' \
  localhost$API/api/v1/incidents/$ID/members/11111111-1111-1111-1111-111111111301

curl -s -H "$AN" -H 'Content-Type: application/json' \
  -d '{"action":"triage","expected_version":1}' \
  localhost$API/api/v1/incidents/$ID/transitions
```
