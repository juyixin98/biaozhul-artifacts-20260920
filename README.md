# FieldSnap — offline field-form sync backend

A Django REST Framework + MySQL backend for field crews that fill in forms
**offline** and sync in batches when connectivity returns. Scope is field
forms only — no health/exercise/diet modules.

## What it does

- **Projects, crews, and versioned form templates.** Up to **150 fields** per
  template; field types `text`, `number`, `enum`, `date`, plus conditional
  required rules (`required_if`). Publishing creates an **immutable snapshot**
  — a published version can never be edited or deleted, and every submission
  keeps a foreign key to the exact version it was collected against.
- **Offline batch sync.** Clients upload batches of up to **50 records**, each
  carrying a client UUID, pinned template version, record version, and
  collection timestamp.
  - Same UUID + same content retried → the **original result is replayed**,
    never a duplicate revision.
  - Same UUID + different content without the current record version →
    **409 conflict; both versions are retained**, nothing is overwritten.
  - Two devices editing the same base version concurrently → both sides kept,
    record enters `conflict`, a **supervisor explicitly resolves** it with a
    recorded note.
- **Template upgrade compatibility.** New fields never invalidate older
  clients; deleted fields / tightened rules use an explicit
  `min_supported_version` floor. Old rows stay stored and readable forever.
- **Incremental pull with a stable cursor.** An append-only change stream
  (`RecordChange`, monotonic BIGINT id) drives sync pulls; pages include
  **tombstones (deletes)** and conflict/resolution markers. Writes that land
  while a client is paging appear on a later page — nothing is skipped or
  duplicated. The cursor is client-side, so a server restart doesn't affect
  resumed syncs.
- **Per-entry results are retained.** Every sync batch and its per-record
  outcomes are queryable afterwards.
- **RBAC.** Workers only touch assigned projects/crews; supervisors resolve
  conflicts only for crews they belong to; admins manage templates and
  assignments.

## Architecture

```
accounts/   users, roles (admin/supervisor/worker), project assignments,
            crew memberships, token auth
forms/      Project, Crew, FormTemplate, immutable FormTemplateVersion,
            field-schema + record validation (validated per PINNED version)
sync/       FormRecord + append-only RecordRevision, SyncBatch/SyncResultEntry,
            RecordChange stream, batch ingestion (services.py),
            advisory-lock concurrency, cursor pulls (pull.py)
```

A record is append-only at the revision level:

```
FormRecord (uuid, status: ok|conflict|deleted, current_revision)
 └── RecordRevision seq=1 (created)
     ├── seq=2 (updated | divergent | stale)      ← sibling branches kept
     └── seq=N (resolved, resolved_by + note)      ← supervisor decision
RecordChange (id BIGINT AUTO_INC, change_type, deleted, ...)  ← pull stream
```

### Concurrency design

Two devices submitting the same UUID at the same instant must serialize
without last-write-wins and without InnoDB duplicate-index deadlocks. The
service takes a **named advisory lock** (`GET_LOCK('fieldsnap:record:<uuid>')`)
per entry, then runs the read-check-write in its **own committed
transaction** (the lock is released only at commit). On SQLite/dev an
in-process lock is used. See `sync/services.py::record_write_lock`.

## Quick start (Docker Compose)

Requirements: Docker + Docker Compose plugin.

```bash
cp .env.example .env          # optional: edit passwords/ports
docker compose up -d --build
```

Services:

| Service | URL / port |
|---|---|
| API | http://localhost:8000 (set `WEB_PORT` to change) |
| Django admin | http://localhost:8000/admin/ |
| MySQL | container `db:3306`, data in volume `fieldsnap_mysql_data` |

Migrations run automatically on container start, and a bootstrap admin is
created (`admin` / `admin12345` — override with `DEFAULT_ADMIN_USERNAME` /
`DEFAULT_ADMIN_PASSWORD`).

Load a demo project, crew, two template versions and users:

```bash
docker compose exec web python manage.py seed_demo
# admin/admin12345, supervisor1/supervisor12345, worker1/worker12345
```

### Running tests

```bash
# against the compose MySQL (includes real two-thread concurrency tests):
docker compose exec web python manage.py test forms sync

# locally without a database (3 concurrency tests skip on SQLite):
python -m venv .venv && source .venv/bin/activate
pip install Django==5.1.4 djangorestframework==3.15.2
DJANGO_DB_ENGINE=sqlite python manage.py test forms sync
```

## API overview

All endpoints except `/api/auth/login/` require
`Authorization: Token <key>`. Per-entry results inside a batch carry their
own `status_code` even though the envelope is HTTP 200.

### Auth

```
POST /api/auth/login/            {"username", "password"} → token + role
GET  /api/auth/me/               current user, project/crew ids
```

Admin only:

```
POST /api/auth/assignments/      {"user", "project"}
POST /api/auth/memberships/      {"user", "crew"}
GET/POST /api/projects/
GET/POST /api/crews/             ?project=<id>
GET/POST /api/templates/         ?project=<id>&code=<code>
POST /api/templates/{id}/versions/publish/
GET  /api/templates/{id}/versions/
GET  /api/templates/{id}/versions/{n}/
```

### Sync

```
POST   /api/sync/submit/                       batch upload (≤ 50 entries)
GET    /api/sync/batches/{client_batch_id}/   retained per-entry results
GET    /api/sync/pull/?cursor=&limit=&project=&template=
GET    /api/sync/conflicts/                    supervisors only
POST   /api/sync/conflicts/{uuid}/resolve/     supervisors only
GET    /api/sync/records/{uuid}/               full revision history
DELETE /api/sync/records/{uuid}/delete/        tombstone (supervisor)
```

See [`API_EXAMPLES.md`](./API_EXAMPLES.md) for a full worked flow.

## Field schema

```json
{
  "key": "crack_count",
  "label": "Crack count",
  "type": "number",
  "required": false,
  "required_if": {"field": "surface", "op": "==", "value": "concrete"}
}
```

- `type`: `text` | `number` | `enum` (requires non-empty unique `options`) |
  `date` (ISO `YYYY-MM-DD`)
- `required_if` operators: `not_blank`, `==`, `!=`, `in`, `not_in`
- Unknown keys in submitted data are rejected for the pinned version (a
  client on an older schema can't smuggle extra fields through).

## Template upgrade policy

1. **Adding fields / options** — publish a new version; old clients stay
   valid because they pin and validate against the old version.
2. **Deleting a field** — historical revisions still render with their
   pinned schema; new submissions on the new version can't send the removed
   key (422).
3. **Tightening rules / retiring a schema** — publish with
   `min_supported_version` set; pinned versions below the floor get a clear
   `minimum supported version` rejection telling the client to upgrade,
   while existing data is never deleted or rewritten.
