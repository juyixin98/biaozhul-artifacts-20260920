# SiteVitals runbook

Concrete operational recipes. For architecture and configuration see
[README.md](../README.md).

## One-minute smoke run (Docker)

```bash
docker compose up --build -d
docker compose exec app /app/sitevitals demo-seed --demo-origin http://demo:8090

# enqueue all three viewports
for vp in mobile tablet desktop; do
  curl -s localhost:8080/api/tasks -H 'content-type: application/json' \
    -d "{\"url\":\"http://demo:8090/normal\",\"viewport\":\"$vp\"}"
done

# watch the queue settle
watch -n1 'curl -s localhost:8080/api/tasks | jq .tasks'

# read the report of task 1
curl -s localhost:8080/api/tasks/1/report | jq -r .markdown
```

## One-minute smoke run (local binary)

```bash
docker run -d -p 3306:3306 --name sv-mysql \
  -e MYSQL_DATABASE=sitevitals -e MYSQL_USER=sitevitals \
  -e MYSQL_PASSWORD=sitevitals -e MYSQL_ROOT_PASSWORD=rootpw mysql:8.4

CHROME_PATH=/usr/bin/google-chrome ENABLE_DEMO=true \
  MYSQL_HOST=127.0.0.1 go run ./cmd/sitevitals serve

# in another shell
go run ./cmd/sitevitals demo-seed --demo-origin http://127.0.0.1:8090
curl -s localhost:8080/api/tasks -H 'content-type: application/json' \
  -d '{"url":"http://127.0.0.1:8090/slow","viewport":"desktop"}'
```

## Failure codes on tasks/runs

| Code | Meaning | Typical cause |
|---|---|---|
| `POLICY_BLOCKED` | Document or redirect hop rejected | non-HTTP scheme, host outside whitelist |
| `REDIRECT_LIMIT` | Main-document redirect chain too long | redirect loop, `MAX_REDIRECTS` |
| `NAVIGATION_TIMEOUT` | `load` event did not fire in time | slow/hanging server (`NAV_TIMEOUT`) |
| `NAVIGATION_FAILED` | Main document network/protocol error | connection refused, DNS failure, reset |
| `BROWSER_CRASH` | Chromium process exited mid-run | renderer/browser crash; instance relaunched |
| `TASK_CANCELLED` | Worker shutdown or lease takeover cancelled the run | graceful restart / reclaim; the task is retried |
| `METRIC_HARVEST_FAILED` | Page loaded but metrics could not be read | DevTools protocol error |
| `BROWSER_LAUNCH_FAILED` | Chromium could not start | missing binary, missing system libs |
| `INTERNAL_ERROR` | Anything unexpected (also used for recovered panics) | bug; details in `error_msg` |

A failed subresource never produces any of these — the run succeeds and the
resource appears with `failed: true` / its HTTP status in the waterfall plus a
summary warning.

## Operational notes

- **Concurrency** = number of Chromium processes (`BROWSER_CONCURRENCY`). Each
  collection uses one tab; tabs are closed when the run finishes. Raising
  concurrency increases memory use roughly linearly; headless Chrome needs
  ~300–500 MB per instance on complex pages.
- **Leases**: if a worker is paused longer than `LEASE_DURATION` (GC pause,
  suspend), another worker may reclaim the task; the old worker detects this
  on the next heartbeat, cancels its browser run and drops its commit.
- **Reaper**: expired leases are also reset to `queued` every 15 s even when
  no worker is actively claiming, so idle fleets still recover crashes.
- **Dead tasks**: after `max_attempts` failures a task moves to `dead`.
  `POST /api/tasks/:id/retry` grants fresh budget. Succeeded tasks cannot be
  retried (409) — enqueue a new task to re-measure; this guarantees one
  report per task.
- **No external calls**: the only network destinations are the MySQL server,
  the whitelisted target sites, and the Chromium DevTools pipe on localhost.
  There is no analytics, telemetry or cloud dependency.

## Manual schema management

Auto-migration runs on boot. For a frozen-schema deployment set
`AUTO_MIGRATE=false` and apply the canonical SQL yourself:

```bash
mysql -h "$MYSQL_HOST" -u "$MYSQL_USER" -p "$MYSQL_DATABASE" < deploy/sql/001_init.sql
# or, using environment configuration:
sitevitals migrate
```
