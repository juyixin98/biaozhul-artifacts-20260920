# RevStream — Ad Waterfall Optimization Backend

A backend for optimizing **client-side ad waterfalls**. The service stores
immutable waterfall configurations, ingests local ad lifecycle events in
idempotent batches, scores ad networks every 30 minutes over a trailing
7-day window, and runs stable 50/50 A/B experiments.

> **Local events only.** Everything in this system is self-contained:
> there are no calls to any ad network SDK/API. "Networks" are a local
> catalog the waterfall refers to by code.

Stack: **Django 5 + Django REST Framework**, **MySQL 8**, **Docker
Compose**, gunicorn. Tests also run on SQLite with no external services.

---

## 1. Quick start (Docker Compose)

```bash
cp .env.example .env          # adjust passwords/secret as needed
docker compose up --build
```

Services:

| Service     | What it does                                                        |
|-------------|--------------------------------------------------------------------|
| `db`        | MySQL 8.4, persistent volume `mysql_data`, healthcheck             |
| `web`       | Django + gunicorn on http://localhost:8000, migrates on boot        |
| `scheduler` | Runs scoring every 30 minutes, aligned to :00/:30 UTC              |

Load sample data (two developers, an app, 4 networks, two published
versions, a running experiment, ~3 days of events, one scoring run):

```bash
docker compose exec web python manage.py seed_demo
```

Demo accounts: `demo_a / password123` (owns `demo-app`) and
`demo_b / password123` (owns nothing, useful to verify isolation).

Run the test suite inside the container (test database needs elevated
DB privileges):

```bash
docker compose exec \
  -e MYSQL_USER=root -e MYSQL_PASSWORD=root_pw \
  web python manage.py test apps
```

### Local development without Docker

```bash
pip install -r requirements.txt   # mysqlclient only needed for MySQL
python manage.py migrate          # defaults to local db.sqlite3
python manage.py seed_demo
python manage.py runserver
python manage.py test apps        # 59 tests, SQLite
```

Set `DB_ENGINE=mysql` plus `MYSQL_*` env vars to use MySQL locally.

### Operations commands

```bash
# score the latest closed 30-minute period (idempotent)
python manage.py score_run

# force recomputation (e.g. after back-filling late events)
python manage.py score_run --force

# recompute every closed period in a range (late-event catch-up)
python manage.py score_run --backfill-from 2026-09-20T00:00:00Z

# one scheduler tick instead of the long-running loop
python manage.py run_scoring_scheduler --once
```

---

## 2. Domain model

```
Developer (auth.User)
 └─ App ── sdk_key (UUID, used by SDK endpoints)
     ├─ Placement ── active_version ──▶ ConfigVersion (immutable)
     │   ├─ PlacementNetwork[]   ← editable draft (priority, cpm_floor,
     │   │                          fallback_order, enabled; max 8)
     │   ├─ ConfigVersion[] ── ConfigVersionEntry[]  (immutable snapshots)
     │   └─ Experiment (≤1 running)
     │       ├─ variant_a_version ─▶ ConfigVersion
     │       ├─ variant_b_version ─▶ ConfigVersion
     │       └─ ExperimentAssignment[] (frozen user→variant)
     └─ AuditLog[] (append-only)

AdNetwork            global catalog (code, display_name)
AdEvent              raw lifecycle events, unique client event_id
ScoreRun / NetworkScore  30-minute scoring runs
```

All monetary columns are `DECIMAL(20,6)` (micro-units, fixed point). The
API accepts decimal **strings** for money (floats are rejected with
`invalid_money`), quantizes with `ROUND_HALF_UP`, and never accepts
negatives.

### Configuration publishing

* The draft (`PlacementNetwork` rows) is freely editable.
* `POST /api/config-versions/publish/` snapshots every **enabled** line
  into a new immutable `ConfigVersion` + `ConfigVersionEntry` rows.
* Validation: 1–8 enabled networks; `priority` exactly `1..N` with no
  gaps/duplicates; `fallback_order` exactly `1..N` similarly.
* Version numbers are gap-free and unique per placement. Publishing takes
  a `SELECT … FOR UPDATE` lock on the placement, recomputes the next
  version number under that lock, inserts the whole snapshot, and **only
  then** flips `Placement.active_version` in the same transaction.
  Readers therefore always see either the previous complete version or
  the new complete version — never a partial one.
* Published versions and entries cannot be updated or deleted (the model
  raises on `.save()`/`.delete()` of existing rows). History is retained
  forever; only the live pointer moves.
* Every mutating action (app/placement/line changes, publish, experiment
  create/stop) writes an append-only `AuditLog` entry with before/after
  payload.

---

## 3. Event ingestion

`POST /api/sdk/events/` (auth: `X-SDK-Key: <app sdk_key>`)

```json
{
  "events": [
    {
      "event_id": "5f0d…",            // client-generated, globally unique
      "event_type": "impression",     // impression | fill | revenue | failure
      "event_time": "2026-09-21T18:00:00Z",
      "placement_code": "rewarded_home",
      "network_code": "applovin",
      "revenue": "0.004250",          // revenue events only; decimal string
      "error_code": "no_fill",        // failure events only
      "config_version_id": 12,        // optional, frozen attribution
      "experiment_id": 3,             // optional
      "experiment_variant": "A",      // requires experiment_id; A|B
      "user_key_hash": "9c1f…"        // optional sha256 hex of stable user id
    }
  ]
}
```

* **Batch limit:** 2000 events (`EVENT_BATCH_MAX_SIZE`); oversize → 400.
* **Idempotency:** `event_id` is unique. An exact replay (same payload) is
  reported as `duplicate` and never double-written. A replay with the same
  id but a *different* payload is a per-item `conflict` error. Duplicate
  ids inside one batch are rejected (`duplicate_in_batch`).
* **Out-of-order & late:** accepted as long as `event_time` is within the
  last 30 days and not >5 min in the future. Scoring windows are derived
  from `event_time`, not arrival time.
* **Per-item results:** the response lists every input with
  `accepted | duplicate | error` (`error_code`, `message`, `field`).
  * all good/duplicates → **202**
  * mixed (some accepted, some errors) → **207**
  * nothing accepted → **400**
* Events may freeze their `config_version_id` and experiment attribution;
  validation ensures the version/experiment belong to the event's
  placement. These references never change retroactively.

---

## 4. Scoring (every 30 minutes, trailing 7 days)

A **period** is a fixed 30-minute UTC bucket (starts at `:00` and `:30`).
The scheduler scores the most recent *closed* period over the window
`[period_start − 7 days, period_end)`. Only events with
`event_time` in that window contribute, so late/out-of-order events flow
into subsequent runs and into explicit backfills automatically.

### Raw component metrics (per placement × network)

Only pairs with **≥ 1 impression** in the window are scored (no sample ⇒
not ranked; never an invented score).

| Component   | Formula                                                        |
|-------------|---------------------------------------------------------------|
| fill rate   | `fills / impressions`                                         |
| eCPM        | `sum(revenue) / impressions × 1000`  (Decimal fixed-point)    |
| reliability | `(fills + revenue_events) / (fills + revenue_events + failures)` |

Notes:

* A network with impressions but zero outcome signals has
  `reliability = NULL` (missing, not zero).
* Zero revenue with impressions yields a *real* eCPM of `0`; it is
  distinct from a missing sample.

### Normalization (per placement)

Each component is min–max scaled **across that placement's scored
networks**:

```
n(x) = (x − min) / (max − min)          → [0, 1]
```

* `max == min` (all equal) ⇒ every network gets **0.5** for that
  component (it carries no ranking signal).
* Networks with no sample for a component (`NULL`) are excluded from the
  min/max and normalize to `NULL`; the weighted sum substitutes a neutral
  **0.5** for that component only. The normalized column stays `NULL` so
  "missing" is never confused with "average".

### Weighted score

```
score = 0.40 × fill_rate_norm
      + 0.35 × ecpm_norm
      + 0.25 × reliability_norm
```

(missing component contributes its weight × 0.5). Weights live in
`settings.SCORE_WEIGHT_*`.

### Tie-breaking (fully deterministic)

1. score desc → 2. raw fill rate desc → 3. raw eCPM desc →
4. impressions desc (more data wins) → 5. network code asc.

All Decimal operations quantize to 6 dp (`ROUND_HALF_UP`).

### Repeatability & atomic switch

* `(period_start)` is unique. Re-running a completed period without
  `force` returns the existing run untouched — duplicate scheduler ticks
  or multiple scheduler replicas are harmless.
* A run stores a **checksum** (sha256 over the ordered score rows);
  recomputing the same window from the same data produces an identical
  checksum (verified in tests).
* A recomputation deletes the old run's rows and inserts the new set in
  one transaction; only after completion does `is_active` flip
  (`ScoreRun.is_active`, exactly one run active globally).
* `score_run --backfill-from …` recomputes historical periods to fold in
  delayed events without affecting newer runs' rows.

---

## 5. A/B experiments (stable 50/50)

* An experiment belongs to one placement and points at two **existing
  immutable ConfigVersions** (`variant_a_version`, `variant_b_version`).
  At most one experiment per placement may be `running` (partial unique
  constraint + row-locked service).
* Bucketing:

  ```
  byte0 = sha256(f"{experiment.salt}:{user_key_hash}")[0]
  variant = "A" if byte0 < 128 else "B"      # exactly 50/50
  ```

  * The salt is a random UUID **per experiment**, so users spread
    independently between experiments.
  * The first resolution is persisted in `ExperimentAssignment` and never
    updated; concurrent first resolutions race on a unique constraint and
    both callers observe the winning row. A user stays in their cohort for
    the experiment's lifetime.
* **Config changes cannot pollute cohorts:** the experiment references
  version *objects*; publishing a new v3 changes nothing about the
  experiment, and events permanently record the `config_version_id` and
  variant used at exposure time. Stats are computed from those frozen
  references.
* Stats caliber (`GET /api/experiments/{id}/stats/`), per variant:
  * `fill_rate = fills / impressions`
  * `eCPM = sum(revenue) / impressions × 1000`
  * a variant with zero impressions reports `fill_rate`/`ecpm` as `null`
    (missing data, not zero), plus assigned-user counts.

SDK flow:

1. `POST /api/sdk/apps/<app_code>/placements/<placement_code>/assign/`
   with `{"user_key_hash": "..."}` → `{experiment_id, variant,
   config_version_id, config_version}`.
2. Fetch that exact config from the config endpoint (or cache it).
3. Send events with `experiment_id`, `experiment_variant`, and
   `config_version_id` attached.

---

## 6. Access control

* Developer API uses **Token authentication**
  (`Authorization: Token …`; obtain via register/login).
* Every collection is filtered to the owning developer; objects belonging
  to someone else return **404** (existence is not revealed), and nested
  creation under a foreign app/placement is rejected.
* SDK endpoints (`/sdk/...`) use the per-app `X-SDK-Key`, never the
  developer token.
* Configuration mutations are recorded in the append-only `AuditLog`
  (scoped to the owner in the API).

---

## 7. HTTP API reference

### Auth
| Method | Path | Notes |
|---|---|---|
| POST | `/api/auth/register/` | `{username,email,password}` → token |
| POST | `/api/auth/login/` | `{username,password}` → token |
| POST | `/api/auth/logout/` | deletes current token |

### Management (Token auth, owner-scoped)
| Method | Path |
|---|---|
| CRUD | `/api/apps/` |
| CRUD | `/api/placements/` |
| CRUD | `/api/network-lines/` (draft lines; `?placement=<id>`) |
| GET | `/api/config-versions/` (`?placement=<id>`, immutable) |
| POST | `/api/config-versions/publish/` `{placement, note?}` |
| GET | `/api/ad-networks/` (global catalog) |
| GET | `/api/audit-logs/` (`?app=&action=`) |
| GET | `/api/score-runs/`, `/api/score-runs/{id}/` |
| GET | `/api/score-runs/active/?placement=<id>` |
| POST | `/api/score-runs/run_now/` `{force?}` |
| CRUD-limited | `/api/experiments/` (create/list/get; no edit) |
| POST | `/api/experiments/{id}/stop/` |
| GET | `/api/experiments/{id}/stats/` |

### SDK (X-SDK-Key)
| Method | Path |
|---|---|
| GET | `/api/sdk/apps/<app_code>/placements/<placement_code>/config/` |
| POST | `…/assign/` |
| POST | `/api/sdk/events/` |

Django admin is available at `/admin/`.

### Example walk-through

```bash
# 1) developer
TOKEN=$(curl -s localhost:8000/api/auth/register/ \
  -H 'Content-Type: application/json' \
  -d '{"username":"alice","password":"password123"}' | jq -r .token)

# 2) app + placement + two draft networks
APP=$(curl -s localhost:8000/api/apps/ -H "Authorization: Token $TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"name":"Game","code":"game"}')
SDK=$(jq -r .sdk_key <<<"$APP"); AID=$(jq -r .id <<<"$APP")
PID=$(curl -s localhost:8000/api/placements/ -H "Authorization: Token $TOKEN" \
  -H 'Content-Type: application/json' \
  -d "{\"app\":$AID,\"code\":\"home\",\"name\":\"Home\",\"format\":\"rewarded\"}" \
  | jq -r .id)
N1=$(curl -s localhost:8000/api/ad-networks/ -H "Authorization: Token $TOKEN" \
  | jq -r '.results[]|select(.code=="admob").id')
# …add network-lines with priority/cpm_floor/fallback_order, then publish…
curl -s -X POST localhost:8000/api/config-versions/publish/ \
  -H "Authorization: Token $TOKEN" -H 'Content-Type: application/json' \
  -d "{\"placement\":$PID,\"note\":\"initial\"}"

# 3) SDK reads live config
curl -s localhost:8000/api/sdk/apps/game/placements/home/config/ \
  -H "X-SDK-Key: $SDK"

# 4) SDK posts events
curl -s -X POST localhost:8000/api/sdk/events/ \
  -H "X-SDK-Key: $SDK" -H 'Content-Type: application/json' \
  -d '{"events":[{"event_id":"e1","event_type":"impression",
      "event_time":"2026-09-21T18:00:00Z","placement_code":"home",
      "network_code":"admob"}]}'
```

---

## 8. Project layout

```
revstream/            Django project (settings, urls, wsgi)
apps/
  accounts/           developer registration / token auth
  catalog/            apps, placements, draft lines, immutable versions,
                      audit log; publishing service; seed command
  ingestion/          batch event API + idempotency/validation service
  scoring/            30-minute runs, normalization, scheduler, commands
  experiments/        50/50 assignment + frozen-cohort stats
  common/             fixed-point money, permissions, error envelope
  tests/              59 tests (run identically on SQLite and MySQL)
docker/               entrypoint (migrate → collectstatic → gunicorn),
                      TCP readiness waiter
```

### Test coverage highlights

* duplicate / idempotent / conflicting / out-of-order / late events,
  per-item batch errors, float-money rejection, batch cap;
* concurrent publishing (MySQL threads + deterministic SQLite
  interleaving), immutable snapshots, complete-before-switch;
* scoring normalization, all-equal → 0.5, missing samples, deterministic
  tie-breaks, repeat-run checksum equality, late-event backfill, single
  active run;
* stable 50/50 distribution (2000 hashes), per-experiment salts, frozen
  cohorts after new publishes, per-variant fill-rate/eCPM caliber;
* cross-developer 404 isolation and audit-log scoping.
