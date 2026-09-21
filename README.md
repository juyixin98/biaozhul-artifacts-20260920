# CommunityVault — Content Revision & Moderation Engine

A small, concurrency-correct backend for moderated user content. Built with
**Go + Chi + sqlc + PostgreSQL**, packaged with **Docker Compose**. There is no
forum and no frontend — only the revision/moderation engine and its HTTP API.

## What it guarantees

1. **Four content states** — `draft → pending → published → withdrawn`. Every
   body change inserts a **new immutable revision**; the old revision is never
   mutated or deleted.
2. **Approvals are bound to one exact revision AND one exact rule version.**
   A review task carries `(revision_id, rule_version_id)`, and every
   `moderation_decisions` row records both. If the author edits after an
   approval, the open task is **cancelled** and content returns to `draft`; the
   old approval can never publish the new body. Publishing additionally checks
   `contents.current_revision_id = approved revision_id`.
3. **Versioned local sensitive-word rules.** Exactly one rule version is
   `active` at a time (partial unique index). Submitting checks the currently
   active word list. Rule activation and moderation/submit decisions take the
   same `pg_advisory_xact_lock`, so a decision can never interleave with a rule
   switch — the bound rule version is deterministic.
4. **Rejections, withdrawals and rollbacks keep a reason.** Rollback copies an
   old revision's body into a **new** revision (reason records the source);
   history is append-only.
5. **Reports are keyed by `(content, reporter)`** with a unique constraint and
   `ON CONFLICT DO NOTHING`; duplicate reports from one user never add a row or
   inflate a count.
6. **Review queue with concurrent claims and timeout reclaim.** Claims use
   `FOR UPDATE SKIP LOCKED`; a claim expires after `CLAIM_TTL_SECONDS`, is
   requeued inline on the next claim and by a background reaper. Completion is
   a conditional `UPDATE ... WHERE state='claimed' AND claimed_by=me AND
   claim_expires_at >= now()`, so an expired claimer cannot overwrite the
   result produced after expiry.
7. **Authorization.** Members edit only their own content. Moderators act only
   inside their assigned categories (`moderator_scopes`). Admin manages rule
   versions. Normal reads return **published revisions only**; hidden content
   returns `404` (existence is not disclosed).
8. **Audit evidence.** `status_events` records every status transition (with
   from/to, revision, rule version, actor, role, reason);
   `moderation_decisions` is the review evidence trail. Reporter identity and
   raw report reasons are visible to in-scope moderators but hidden from
   authors (authors get aggregate counts only).
9. **Stable-cursor feed.** Keyset pagination over `(updated_at DESC, id DESC)`
   using an opaque base64 cursor, and the query selects only
   `status='published'` rows. Rows created or withdrawn while a reader pages
   can never leak unpublished content or reappear/duplicate.

## Project layout

```
cmd/server/          main: migrate on boot, serve HTTP, run claim reaper
internal/
  auth/              X-User-ID dev auth, roles, moderator scopes
  config/            env configuration
  db/                sqlc-generated queries and models
  httpapi/           Chi router + JSON handlers (+ HTTP integration tests)
  httpx/             JSON helpers
  migrate/           embedded SQL migration runner (+ migrations/*.sql copy)
  service/           business logic / transactions (+ concurrency tests)
  testdb/            isolated per-package test database helper
db/queries/*.sql     sqlc source queries
migrations/*.sql     schema + seed (authoritative; copied to internal/migrate)
sqlc.yaml
docker-compose.yml   postgres + api
Dockerfile           multi-stage Go build
scripts/demo.sh      full end-to-end walkthrough
```

## Quick start (Docker Compose)

```bash
docker compose up --build
# API: http://localhost:8080   Postgres: localhost:5432
```

The `api` container waits for the database health check, applies all embedded
migrations automatically on startup, and then serves.

Seeded identities (pass via the dev-only `X-User-ID` header):

| ID | User      | Role      | Scope |
|----|-----------|-----------|-------|
| 1  | alice     | member    | —     |
| 2  | bob       | member    | —     |
| 3  | mod_tech  | moderator | tech  |
| 4  | mod_art   | moderator | art   |
| 5  | admin     | admin     | all   |

The baseline active rule version (`v1`) contains one word: `forbidden`.

## Local development without Docker

Requires Go 1.22+, a running Postgres 14+, and `sqlc` only when changing
queries.

```bash
# 1. database
docker run --name cv-db -e POSTGRES_USER=community -e POSTGRES_PASSWORD=community \
  -e POSTGRES_DB=communityvault -p 5432:5432 -d postgres:16-alpine

# 2. regenerate query code (only if you edit db/queries/*.sql or migrations)
go install github.com/sqlc-dev/sqlc/cmd/sqlc@v1.27.0
sqlc generate
cp migrations/*.sql internal/migrate/   # keep embedded copies in sync

# 3. run (migrations apply on boot)
DATABASE_URL='postgres://community:community@localhost:5432/communityvault?sslmode=disable' \
HTTP_ADDR=':8080' CLAIM_TTL_SECONDS=30 go run ./cmd/server
```

Environment variables:

| Variable           | Default                                                                 | Meaning                          |
|--------------------|-------------------------------------------------------------------------|----------------------------------|
| `DATABASE_URL`     | `postgres://community:community@localhost:5432/communityvault?sslmode=disable` | Postgres DSN         |
| `HTTP_ADDR`        | `:8080`                                                                 | Listen address                   |
| `CLAIM_TTL_SECONDS`| `30`                                                                    | Review claim validity / reaper   |

### Running the tests

Integration tests need Postgres; they create and migrate two throwaway
databases (`cv_test_service`, `cv_test_httpapi`) automatically.

```bash
# point at your Postgres instance (any db in the cluster works; tests DROP/CREATE their own)
export TEST_DATABASE_URL='postgres://community:community@localhost:5432/communityvault?sslmode=disable'
go test -race ./...
# fail loudly instead of skipping when no DB is reachable:
TEST_DB_REQUIRED=1 go test -race ./...
```

Covered scenarios: full lifecycle, stale approval after edit, member/moderator
authorization, read visibility by state, submit-time rule rejection, rule
version switch binding, duplicate-report dedup + privacy views, concurrent
claims (SKIP LOCKED), expired claim cannot complete, concurrent edit vs approve
(lock ordering + deadlock retry), concurrent rule switch vs submit, and stable
feed paging across a mid-page withdrawal.

## API

Authentication for this exercise is the dev-only header `X-User-ID: <id>`.
All bodies are JSON.

### Content (authors)

| Method | Path                                   | Notes                                              |
|--------|----------------------------------------|----------------------------------------------------|
| POST   | `/v1/contents`                         | create draft + revision #1 `{category,title,body}` |
| PATCH* | `/v1/contents/{id}/edits`              | new revision; pending posts go back to draft       |
| POST   | `/v1/contents/{id}/submit`             | validate vs active rules → `pending` (or 422)      |
| POST   | `/v1/contents/{id}/rollback`           | `{revision_id, reason}` → new revision, old kept   |
| POST   | `/v1/contents/{id}/withdraw`           | `{reason}` (mandatory) → `withdrawn`               |
| GET    | `/v1/contents/{id}`                    | published for all; hidden only to author/mod/admin |
| GET    | `/v1/contents/{id}/revisions`          | revision history (visibility-checked)              |
| GET    | `/v1/contents`                         | public feed `?limit=&cursor=`                      |

\* edits use `POST .../edits` with `{title, body, edit_reason}`.

### Reports

| Method | Path                                | Notes                                          |
|--------|-------------------------------------|------------------------------------------------|
| POST   | `/v1/contents/{id}/reports`         | `{reason}`; duplicates return `created:false`  |
| GET    | `/v1/contents/{id}/reports`         | mods: full; author: `{total,open}` aggregates  |
| POST   | `/v1/reports/{id}/resolve`          | in-scope moderator/admin                       |

### Moderation queue

| Method | Path                              | Notes                                                |
|--------|-----------------------------------|------------------------------------------------------|
| GET    | `/v1/mod/queue`                   | open tasks in the moderator's categories             |
| POST   | `/v1/mod/claims`                  | claim next task (`SKIP LOCKED`); 204 if none         |
| POST   | `/v1/mod/tasks/{id}/approve`      | `{reason?}` publishes the **bound** revision         |
| POST   | `/v1/mod/tasks/{id}/reject`       | `{reason}` (mandatory) → back to draft               |
| GET    | `/v1/contents/{id}/decisions`     | review evidence trail                                |
| GET    | `/v1/contents/{id}/events`        | status transition audit trail                        |

### Rule versions (admin)

| Method | Path             | Notes                                          |
|--------|------------------|------------------------------------------------|
| GET    | `/v1/rules`      | all versions with words (admin)                |
| GET    | `/v1/rules/active` | current version + words (moderators too)     |
| POST   | `/v1/rules`      | `{description, words:[]}` activates new version |

## Complete worked example

Run the full scripted walkthrough (creates content, rejects, approves, reports,
switches rules, demonstrates stale-approval invalidation, rollback and the
audit trail) against a running server:

```bash
scripts/demo.sh http://localhost:8080
```

Manual minimal session:

```bash
# alice writes and submits
curl -s -H 'X-User-ID: 1' -H 'Content-Type: application/json' \
  -d '{"category":"tech","title":"hi","body":"clean text"}' \
  localhost:8080/v1/contents
curl -s -X POST -H 'X-User-ID: 1' localhost:8080/v1/contents/1/submit

# tech moderator claims and approves -> published
curl -s -X POST -H 'X-User-ID: 3' localhost:8080/v1/mod/claims
curl -s -X POST -H 'X-User-ID: 3' -H 'Content-Type: application/json' \
  -d '{"reason":"ok"}' localhost:8080/v1/mod/tasks/1/approve

# public feed
curl -s -H 'X-User-ID: 2' 'localhost:8080/v1/contents'
```

## Concurrency design notes

- **Rule-vs-decision determinism:** `pg_advisory_xact_lock(<constant>)` is the
  first statement in both rule activation and submit/approve transactions,
  fully serializing them; each task/decision snapshots the rule version id it
  was judged under.
- **Claim fan-out:** `FOR UPDATE SKIP LOCKED` on the queue lets many
  moderators claim distinct tasks at once; the candidate is filtered by the
  claimant's category array.
- **Claim expiry:** completion predicates include `claim_expires_at >= now()`.
  Reclaiming re-queues expired rows; the original claimer's late write affects
  zero rows and gets `409 claim expired`.
- **Edit-vs-approve ordering:** all transactions lock the `contents` row before
  the `review_tasks` row, eliminating the lock-order deadlock; residual
  serialization failures (`40P01/40001/55P03`) are retried by the tx helper.
- **Feed stability:** query-time predicate `status='published'` + row-value
  keyset `(updated_at, id)` makes withdrawals/additions during paging safe.
