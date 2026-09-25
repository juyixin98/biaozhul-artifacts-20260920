# HTTP API reference

All request and response bodies are JSON (except `text/plain` ingest). The
server binds `:8080` by override with `-addr`. No authentication is included —
this is a local single-node sample.

Start it:

```bash
make build
./bin/logcluster -addr :8080 -snapshot data/snapshot.json
```

## `GET /healthz`

Liveness probe.

```bash
curl -s localhost:8080/healthz
# {"status":"ok"}
```

## `POST /v1/ingest`

Ingest one line (`line`) or a batch (`lines`). Accepts:

- `application/json` — object `{"line": "..."}` or `{"lines": ["...", ...]}`
- `text/plain` — one log line per newline

Each line is tokenized, assigned to a template cluster and stored in the
bounded event ring. The response reports the assigned cluster, the (possibly
evolved) template and its current version.

```bash
curl -s -X POST localhost:8080/v1/ingest \
  -H 'Content-Type: application/json' \
  -d @examples/ingest-one.json
```

```json
{
  "ingested": 1,
  "rejected": 0,
  "results": [
    {
      "seq": 1,
      "line": "2026-09-24 10:00:00.001 INFO http: GET /api/users/550e8400-... 200 1024 bytes in 12ms",
      "cluster_id": 1,
      "template": "<NUM>-<NUM>-<NUM> <NUM>:<NUM>:<NUM> INFO http: GET /api/users/<UUID> <NUM> <NUM> bytes in <NUM>",
      "version": 1
    }
  ]
}
```

Plain text (one line per `\n`, blank lines ignored):

```bash
printf '2026-09-24 10:00:00 INFO one 5\n2026-09-24 10:00:01 INFO two 6\n' \
  | curl -s -X POST localhost:8080/v1/ingest -H 'Content-Type: text/plain' --data-binary @-
```

Numbers mask as `<NUM>`, UUIDs as `<UUID>`, quoted strings as `<STR>`, and
generic evolved slots as `<*>`. Literal keywords never mask, so
`... refused ...` and `... timed out ...` are always different templates.

## `GET /v1/templates`

All active template clusters, ordered by ID.

```bash
curl -s localhost:8080/v1/templates | jq
```

Each entry carries `id`, `template`, `version`, `count`, time bounds, up to
three bounded `samples`, and the full `versions` history (every revision with
its number, rendered template, timestamp and promotion reason).

## `GET /v1/templates/{id}`

One cluster including its version history:

```bash
curl -s localhost:8080/v1/templates/6 | jq '.versions'
```

## `GET /v1/events`

Recent stored events, newest first. Query parameters:

- `q=<substring>` — case-sensitive substring filter on the raw line
- `cluster_id=<n>` — restrict to one cluster
- `limit=<n>` — page size (default 100, max 1000)

```bash
curl -s 'localhost:8080/v1/events?q=refused'
curl -s 'localhost:8080/v1/events?cluster_id=2&limit=20'
```

## `GET /v1/evicted`

The bounded log of templates retired under cluster-capacity pressure (LRU),
with their last observed counts and eviction timestamps.

## `GET /v1/metrics`

Counters: total/truncated line counts, active cluster count and capacity,
cumulative evictions, stored events and ring capacity.

```bash
curl -s localhost:8080/v1/metrics
```

## `POST /v1/snapshot`

Persist the full state (clusters, version history, events, counters) to a
local JSON file atomically (temp file + rename). The server also autosaves on
a timer (`-autosave`, default 30s) and on graceful shutdown (SIGINT/SIGTERM).

```bash
curl -s -X POST localhost:8080/v1/snapshot \
  -H 'Content-Type: application/json' \
  -d '{"path":"data/snapshot.json"}'
```

The snapshot is loaded automatically at startup when `-snapshot` points at an
existing file.

## Status codes

- `200` OK
- `400` malformed JSON, empty ingest, invalid query parameter
- `404` unknown template id
- `415` unsupported content type
