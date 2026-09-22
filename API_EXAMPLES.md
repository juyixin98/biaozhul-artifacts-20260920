# FieldSnap API examples

All examples assume the stack is running and the demo data is seeded:

```bash
docker compose up -d --build
docker compose exec web python manage.py seed_demo
BASE=http://localhost:8000   # or ${WEB_PORT}
```

## 1. Login

```bash
curl -s -X POST $BASE/api/auth/login/ \
  -H 'Content-Type: application/json' \
  -d '{"username":"worker1","password":"worker12345"}'
```

```json
{
  "id": 3,
  "username": "worker1",
  "role": "worker",
  "display_name": "worker1",
  "token": "9f2c…",
  "project_ids": [1],
  "crew_ids": [1]
}
```

```bash
WT=...   # worker token
ST=...   # supervisor token
AUTH_W="Authorization: Token $WT"
```

## 2. Fetch the current template (what the device pins offline)

```bash
# list templates
curl -s "$BASE/api/templates/?project=1" -H "$AUTH_W"

# fetch immutable snapshot v1 and store it on the device
curl -s "$BASE/api/templates/1/versions/1/" -H "$AUTH_W"
```

```json
{
  "id": 1,
  "version": 1,
  "min_supported_version": 1,
  "fields": [
    {"key": "structure_id", "label": "Structure ID", "type": "text", "required": true},
    {"key": "surface", "label": "Surface", "type": "enum",
     "options": ["concrete", "steel", "wood"], "required": true},
    {"key": "crack_count", "label": "Crack count", "type": "number",
     "required_if": {"field": "surface", "op": "==", "value": "concrete"}},
    {"key": "inspection_date", "label": "Date", "type": "date", "required": true},
    {"key": "notes", "label": "Notes", "type": "text"}
  ]
}
```

## 3. Offline batch submit

Each entry: client `uuid`, `template_id`, pinned `template_version`, the last
`record_version` the device knew (0 on create), `crew_id`, `collected_at` and
`data`.

```bash
curl -s -X POST $BASE/api/sync/submit/ -H "$AUTH_W" \
  -H 'Content-Type: application/json' \
  -d '{
    "client_batch_id": "device-a-20260921-01",
    "entries": [
      {
        "uuid": "11111111-1111-4111-8111-111111111111",
        "template_id": 1,
        "template_version": 1,
        "record_version": 0,
        "crew_id": 1,
        "collected_at": "2026-09-21T08:30:00Z",
        "data": {
          "structure_id": "B-017",
          "surface": "concrete",
          "crack_count": 4,
          "inspection_date": "2026-09-21",
          "notes": "spalling near pier 2"
        }
      }
    ]
  }'
```

```json
{
  "batch_id": 1,
  "client_batch_id": "device-a-20260921-01",
  "replayed": false,
  "entries": [
    {
      "client_uuid": "11111111-…",
      "status": "accepted",
      "status_code": 201,
      "record_version": 1,
      "revision_seq": 1,
      "content_hash": "a81d…",
      "conflict_with_seq": null,
      "errors": null
    }
  ]
}
```

### 3a. Network retry — same batch, same content

Re-POST the identical `client_batch_id` (or the same UUID+content in a new
batch) after a timeout. The stored result is replayed (`201`, version 1);
no second revision is created and the response contains `replayed: true`.

### 3b. Fast-forward update (device saw record_version 1)

```json
{
  "uuid": "11111111-…",
  "template_version": 1,
  "record_version": 1,
  "data": {"…": "…", "notes": "note edited after follow-up"}
}
```
→ `status_code: 200, record_version: 2`.

### 3c. Concurrent edit from device B (still on record_version 1/0)

Different content against an older base → both versions kept:

```json
{
  "status": "conflict",
  "status_code": 409,
  "record_version": 3,
  "conflict_with_seq": 2,
  "errors": {"__all__": "concurrent_modification: your edit branched from v1 but v2 exists; both versions kept, awaiting supervisor resolution"}
}
```

### 3d. Validation rejection never blocks the batch

```json
{"status": "rejected", "status_code": 422,
 "errors": {"surface": "must be one of: concrete, steel, wood"}}
```

### 3e. Replay retained results

```bash
curl -s $BASE/api/sync/batches/device-a-20260921-01/ -H "$AUTH_W"
```

## 4. Supervisor resolves the conflict

```bash
curl -s "$BASE/api/sync/conflicts/" -H "Authorization: Token $ST"
```

```bash
curl -s -X POST \
  "$BASE/api/sync/conflicts/11111111-1111-4111-8111-111111111111/resolve/" \
  -H "Authorization: Token $ST" -H 'Content-Type: application/json' \
  -d '{
    "merged_data": {
      "structure_id": "B-017",
      "surface": "concrete",
      "crack_count": 5,
      "inspection_date": "2026-09-21",
      "notes": "merged: spalling + device-B crack recount"
    },
    "template_version": 1,
    "note": "Kept A structure/date, used B crack count verified on site photo."
  }'
```

The resolution becomes a new authoritative revision (`resolved`); the
original two sides remain in `/api/sync/records/{uuid}/` with full history,
including who resolved it and why.

## 5. Incremental pull

First call has no cursor; afterwards pass back the returned `next_cursor`.

```bash
curl -s "$BASE/api/sync/pull/?limit=100" -H "$AUTH_W"
```

```json
{
  "changes": [
    {
      "cursor_id": 1,
      "record_uuid": "11111111-…",
      "change_type": "created",
      "record_status": "ok",
      "deleted": false,
      "project_id": 1, "crew_id": 1, "template_id": 1,
      "record_version": 1,
      "template_version": 1,
      "data": {"…": "…"},
      "collected_at": "2026-09-21T08:30:00+00:00",
      "changed_at": "2026-09-21T00:30:00.000000+00:00"
    },
    {"cursor_id": 3, "change_type": "conflict", "record_status": "conflict", "…": "…"},
    {"cursor_id": 4, "change_type": "resolved", "record_status": "ok", "…": "…"}
  ],
  "next_cursor": "NA==",
  "has_more": false
}
```

A delete shows up as a tombstone — clients must remove the row locally:

```json
{"change_type": "deleted", "deleted": true, "record_version": null, "data": null}
```

The cursor is opaque and lives on the client. A new writer that commits
while you're paging simply appears on the next page; pages never skip or
repeat entries.

## 6. Publishing a new template version (admin)

```bash
curl -s -X POST $BASE/api/templates/1/versions/publish/ \
  -H "Authorization: Token $ADMIN_TOKEN" -H 'Content-Type: application/json' \
  -d '{
    "fields": [
      {"key": "structure_id", "label": "Structure ID", "type": "text", "required": true},
      {"key": "surface", "label": "Surface", "type": "enum",
       "options": ["concrete","steel","wood"], "required": true},
      {"key": "crack_count", "label": "Crack count", "type": "number"},
      {"key": "inspection_date", "label": "Date", "type": "date", "required": true},
      {"key": "gps_lat", "label": "Latitude", "type": "number"},
      {"key": "gps_lon", "label": "Longitude", "type": "number"}
    ]
  }'
```

Retiring a schema (old devices receive an explicit upgrade error, old data
is preserved):

```json
{"fields": [ … ], "min_supported_version": 2}
```

## Status codes inside a batch

| `status_code` | Meaning |
|---|---|
| 201 | Created (first revision) |
| 200 | Updated (fast-forward) |
| 409 | Conflict — divergent sibling stored, supervisor must resolve; or tombstone collision |
| 422 | Validation rejection against the pinned template version |
| 403 | Not a member of the target crew (inside the entry) |
| 400 | Malformed entry |
