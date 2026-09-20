# SiteVitals

SiteVitals measures web performance for **registered intranet sites** with a
real, locally-run **Chromium** browser over the **Chrome DevTools Protocol
(CDP)**. No cloud services are involved: the API, the worker, MySQL and
Chromium all run locally (or in your own Docker host).

Features:

- **Whitelisted sites & URLs** with three device viewports (mobile / tablet /
  desktop). Only `http`/`https` is accepted; every redirect hop **and** every
  page subresource is checked against the whitelist. Off-list subresources are
  recorded as violations and optionally blocked.
- **Real browser metrics** via CDP: navigation duration, FCP, LCP, CLS, long
  tasks and a per-resource waterfall (DNS/connect/SSL/TTFB/download phases).
  Unsupported or failed metrics are stored as `NULL` with an explicit status
  (`ok` / `partial` / `unsupported` / `failed`) — never silently zero.
- **Durable task queue in MySQL** with bounded browser concurrency. Task
  claims carry a rotating **lease token** and heartbeat; a crashed worker's
  tasks are reclaimed; a late commit from the old executor is rejected; a
  successful task gets exactly one report.
- **Classified failures**: `NAVIGATION_TIMEOUT`, `BROWSER_CRASH`,
  `NAVIGATION_FAILED`, `POLICY_BLOCKED`, `REDIRECT_LIMIT`,
  `METRIC_HARVEST_FAILED`. Per-resource failures are recorded but do not fail
  the task. Browser tabs/processes are always released and a crashed browser
  is relaunched, so one bad task never blocks the queue.
- **Comparisons & budgets**: compare two successful runs of the same URL +
  viewport, and evaluate actual values against thresholds; breaches store the
  actual value, threshold and linked run. Failed runs never enter successful
  metric statistics.

## Quick start (Docker)

```bash
docker compose up --build
# (in another shell) register the built-in demo site against the demo container:
docker compose exec app /app/sitevitals demo-seed --demo-origin http://demo:8090
```

Then submit tasks against the API (from a shell inside the Docker network the
browser resolves `demo`; from the host you can instead run the local Go binary
with `ENABLE_DEMO=true` and whitelist `http://127.0.0.1:8090`, see below):

```bash
curl -s localhost:8080/api/tasks -H 'content-type: application/json' \
  -d '{"url":"http://demo:8090/normal","viewport":"mobile"}'
curl -s localhost:8080/api/tasks
curl -s localhost:8080/api/tasks/1/report | jq -r .Markdown
```

Services exposed by `docker-compose.yml`:

| Service | Port | Role |
|---|---|---|
| `app`  | 8080 | Gin API + worker (launches Chromium internally) |
| `demo` | 8090 | Demo "intranet" site (slow pages, CLS, long tasks, 404/500 subresources, redirect chains) |
| `mysql`| 3306 | MySQL 8.4 with persistent volume |

## Local development run

Requires Go 1.22+, a local Chromium/Chrome, and MySQL 8.

```bash
# 1. start MySQL however you like, e.g.
docker run -d --name sv-mysql -p 3306:3306 \
  -e MYSQL_DATABASE=sitevitals -e MYSQL_USER=sitevitals \
  -e MYSQL_PASSWORD=sitevitals -e MYSQL_ROOT_PASSWORD=rootpw mysql:8.4

# 2. run API + worker + demo site in one process
export CHROME_PATH=/usr/bin/google-chrome
export ENABLE_DEMO=true
go run ./cmd/sitevitals serve

# 3. whitelist the demo site and enqueue
./sitevitals demo-seed --demo-origin http://127.0.0.1:8090
curl -s localhost:8080/api/tasks -H 'content-type: application/json' \
  -d '{"url":"http://127.0.0.1:8090/slow","viewport":"desktop"}'
```

## Configuration (environment variables)

| Variable | Default | Meaning |
|---|---|---|
| `MYSQL_DSN` | — | Full DSN; overrides the `MYSQL_*` parts below |
| `MYSQL_HOST` / `MYSQL_PORT` | `127.0.0.1` / `3306` | MySQL location |
| `MYSQL_USER` / `MYSQL_PASSWORD` / `MYSQL_DATABASE` | `sitevitals` | Credentials |
| `HTTP_ADDR` | `:8080` | API listen address |
| `DEMO_ADDR` | `:8090` | Demo site listen address (only with `ENABLE_DEMO=true`) |
| `ENABLE_DEMO` | `false` | Serve the built-in demo site in-process |
| `ENABLE_WORKER` | `true` | Run the browser worker pool in this process |
| `CHROME_PATH` | auto-detect | Chromium/Chrome executable |
| `CHROMIUM_WS_URL` | — | Attach to an existing Chrome over CDP instead of launching |
| `HEADLESS` | `true` | Headless Chromium |
| `BROWSER_CONCURRENCY` | `2` | Number of browser instances / max parallel measurements |
| `LEASE_DURATION` | `90s` | Task lease length; expiry allows crash recovery |
| `HEARTBEAT_INTERVAL` | `10s` | Lease renewal cadence while a measurement runs |
| `NAV_TIMEOUT` | `30s` | Per-navigation timeout (`NAVIGATION_TIMEOUT` failure) |
| `TASK_TIMEOUT` | `60s` | Hard cap for one task attempt |
| `SETTLE_TIME` | `3s` | Post-load observation window before harvesting metrics |
| `MAX_REDIRECTS` | `10` | Main-document redirect chain bound (`REDIRECT_LIMIT`) |
| `STRICT_SUBRESOURCES` | `false` | Block (instead of just record) off-list subresources |
| `AUTO_MIGRATE` | `true` | Apply schema on startup |
| `WORKER_RUN_ONCE` | `false` | Worker processes at most one task then exits (CI/demo) |

## API overview

All bodies are JSON.

| Method & path | Purpose |
|---|---|
| `GET  /healthz` | Liveness + DB ping |
| `GET/POST /api/sites` | List / register allowed sites (`origin` = scheme://host[:port], `path_prefix`) |
| `GET/PUT/DELETE /api/sites/:id` | Manage one site |
| `POST /api/tasks` | Enqueue `{url, viewport: mobile\|tablet\|desktop, priority?, max_attempts?}` |
| `GET  /api/tasks?status=` | List tasks |
| `GET  /api/tasks/:id` | Task state, owner, attempts, lease and error classification |
| `POST /api/tasks/:id/retry` | Re-queue failed/dead task (409 for succeeded tasks) |
| `GET  /api/tasks/:id/runs` | Every attempt (succeeded/failed/abandoned) with diagnostics |
| `GET  /api/tasks/:id/report` | The single markdown report for a successful task |
| `GET  /api/runs/:id` | Full run incl. metric statuses, violations, waterfall |
| `POST /api/compare` | `{baseline_run_id, current_run_id}` — same URL + viewport, both succeeded |
| `GET  /api/comparisons` | Recent comparisons (a successful run also auto-compares to the previous one) |
| `GET/PUT /api/budgets/global` | Read/set default thresholds |
| `PUT  /api/budgets/sites/:id` | Per-site threshold override |
| `GET  /api/alerts?run_id=` | Budget breaches with actual value, threshold and linked run |
| `GET  /api/stats?url=&viewport=` | Averages over **successful runs only** (NULL metrics excluded per metric) |

## Metrics: collection semantics & honest statuses

A script (`addScriptToEvaluateOnNewDocument`) is installed **before any page
script runs** and sets up buffered `PerformanceObserver`s for:

- `paint` → **FCP**
- `largest-contentful-paint` → **LCP** (last candidate at harvest)
- `layout-shift` → **CLS** (excluding shifts after recent input)
- `longtask` → count / total / max of main-frame tasks longer than 50 ms
- Navigation Timing Level 2 → **navigation duration** (`loadEventEnd − startTime`)
- CDP `Network` domain events + `ResourceTiming` → the **waterfall**

**Long-task observation window**: long tasks are observable only while the
injected observer is alive — from document creation until the harvest call
(navigation + `SETTLE_TIME`, default 3 s after `load`). Each run records
`window_start_offset_ms` (0) and `window_end`; tasks occurring after harvest
are out of scope and that is stated in the report. Cross-origin iframes do not
attributable long tasks to the parent frame.

Every metric has a status stored alongside it (`metric_status_json`):

| Status | Meaning |
|---|---|
| `ok` | Measured via the real browser API |
| `partial` | Measured but restricted (e.g. cross-origin resource timing without `Timing-Allow-Origin`, or harvest before load settled) |
| `unsupported` | Browser did not expose the API; value stays `NULL` |
| `failed` | API present but no value produced; value stays `NULL` |

A legitimate zero (e.g. CLS 0) is stored as `0` with status `ok`; genuine
absence is `NULL`. Averages skip `NULL`s per metric.

## Whitelist enforcement

- The submitted URL must parse as `http`/`https` and match an enabled site
  entry (exact scheme, host, port, and path-prefix; `*.label` wildcard hosts
  supported). It is normalized (lowercased host, default port stripped, query
  sorted, fragment removed) and the normalized form is the comparison key.
- CDP `Fetch` interception is enabled at the **Request** stage for every
  resource type, so:
  - the main document and **every redirect hop** are checked — a `file:` /
    `data:` hop or a hop to a non-whitelisted host aborts navigation with
    `POLICY_BLOCKED` and a violation row;
  - page **subresources** are checked too. Off-list loads are always recorded
    as violations; with `STRICT_SUBRESOURCES=true` they are failed in the
    browser (HTTP-style block), otherwise allowed to load so measurement
    continues.
- Redirect chains beyond `MAX_REDIRECTS` are blocked with `REDIRECT_LIMIT`
  (also bounds infinite redirect loops).

## Queue, leases and crash recovery

- Each task is a durable row (`queued → running → succeeded|failed|dead`).
- `Claim` runs in one InnoDB transaction:
  `SELECT … FOR UPDATE SKIP LOCKED` over due queued tasks and expired leases,
  inserts a run row for the attempt, and rotates owner + `lease_token` +
  `leased_until`. Concurrent workers therefore never double-claim.
- While measuring, the worker heartbeats; if the heartbeat fails (the task was
  reclaimed), the in-flight browser run is cancelled and its commit dropped.
- If a process dies, the lease expires; a later claim marks the old run
  `abandoned` (`LEASE_EXPIRED`), increments the attempt and proceeds. A
  background reaper also returns expired leases to `queued`.
- All success/failure commits are guarded by both the lease token **and** the
  run's `running` status inside a row-locked transaction — a late commit from
  the dead executor updates zero rows.
- `reports.task_id` and `reports.run_id` are unique: exactly one report per
  task, and retrying a succeeded task returns 409 (re-measuring is a new
  task).

## Migrations

Schema is applied automatically on boot (`AUTO_MIGRATE=true`). For manual or
reviewed deploys:

```bash
# Creates the database if it does not exist, then applies the schema:
sitevitals migrate
# …or apply the canonical SQL yourself (also self-provisions the database):
mysql -h "$MYSQL_HOST" -u root -p < deploy/sql/001_init.sql
```

Migration policy: additive changes ship through GORM AutoMigrate; destructive
or data-changing changes get a new numbered file in `deploy/sql/`.

## Tests

```bash
# Fast pure unit tests (whitelist, comparison, budgets): no DB or browser.
go test -short ./...

# Queue/lease tests against real MySQL 8 (InnoDB semantics matter):
# either point at a running MySQL:
TEST_MYSQL_DSN='sv:sv@tcp(127.0.0.1:13399)/sitevitals_test?parseTime=true&loc=UTC' \
  go test ./internal/store/ ./internal/api/
# …or let testcontainers start mysql:8.4 itself (Docker access required):
go test ./internal/store/ ./internal/api/

# Real-Chromium browser tests (demo site served in-process by httptest):
go test ./internal/browser/

# Full worker end-to-end (needs BOTH MySQL and Chromium):
TEST_MYSQL_DSN='sv:sv@tcp(127.0.0.1:13399)/sitevitals_test?parseTime=true&loc=UTC' \
  go test ./internal/worker/
```

Set `SKIP_CHROME_TESTS=1` to skip tests that launch Chromium.

## Demo site

The built-in demo (`internal/demo`) intentionally produces measurable
behavior:

| Path | Demonstrates |
|---|---|
| `/normal` | Fast baseline |
| `/slow` | Render-blocking slow CSS + slow image |
| `/cls` | Late content injection → layout shift |
| `/longtask` | Four ~120 ms main-thread tasks |
| `/partial` | 404 image + 500 subresource, page still succeeds |
| `/redirect/1` → `/redirect/2` → `/redirect/final` | In-whitelist redirect chain |
| `/badredirect-file` | Redirect hop to `file://` → blocked |
| `/badredirect-offsite` | Redirect hop off the whitelist → blocked |
| `/redirect/loop/…` | Infinite redirect loop → `REDIRECT_LIMIT` |
| `/hang` | Document never finishes → `NAVIGATION_TIMEOUT` |

## Not implemented / scope boundaries

- No authentication/multi-tenancy on the API (trusted intranet deployment).
- No cloud connectivity, APM or external Lighthouse; metrics come straight
  from CDP/Performance APIs.
- Trace/filmstrip/screenshot capture is not persisted; the waterfall is
  timing data only.
