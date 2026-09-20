# ConsentVault

An attributable consent-record backend: it keeps a faithful, auditable history
of **grants, withdrawals and expirations** of consent, organized by
**organization / subject / purpose**, and answers "is this consent currently
valid, and on what basis?".

> **Scope note.** ConsentVault records *attributable consent state*. It does
> **not** claim to certify compliance with any specific regulation
> (GDPR/ePrivacy/CCPA/…). Legal conformance depends on how an organization
> operates the service and the policies it publishes.

Built with **FastAPI + SQLAlchemy 2.0 + PostgreSQL 16**, runnable with Docker.

---

## Quick start (Docker)

```bash
docker compose up --build
# API:        http://localhost:8000
# Swagger UI: http://localhost:8000/docs
```

The container waits for PostgreSQL, runs Alembic migrations, seeds two demo
organizations + API keys and a small scenario, then starts uvicorn.

Demo credentials (printed in container logs and fixed in `docker-compose.yml`):

| Organization | Admin key | Auditor key |
|---|---|---|
| `demo-org` | `demo-admin-key` | `demo-auditor-key` |
| `demo-org-2` | `demo-org2-admin-key` | `demo-org2-auditor-key` |

Run the guided walkthrough against the running stack:

```bash
./scripts/demo.sh
```

## Local development

```bash
python3 -m venv .venv && source .venv/bin
pip install -r requirements.txt

# point at a PostgreSQL and run migrations
export CV_DATABASE_URL=postgresql+psycopg2://USER:PASS@localhost:5432/consentvault
alembic upgrade head
python -m app.bootstrap            # optional demo seed
uvicorn app.main:app --reload

# tests (dedicated database)
export CV_DATABASE_URL=postgresql+psycopg2://USER:PASS@localhost:5432/consentvault_test
alembic upgrade head
pytest
```

---

## Core model

`consent_events` is the **single source of truth** — an append-only ledger.
`consent_states` is a **derived** materialization (one row per
org/subject/purpose) maintained transactionally and rebuildable at will.

```
grant / withdraw event  ──append──▶  consent_events (immutable)
                                         │ deterministic fold (ordered by id)
                                         ▼
                                   consent_states (cache; rebuildable)
                                         │ query-time expiry evaluation
                                         ▼
                                   GET .../consents/{purpose}
```

* **Policy versions** (`policy_versions`) are immutable once published and
  protected by PostgreSQL triggers that reject `UPDATE`/`DELETE`.
* Every grant pins a concrete `policy_version`. Publishing a new version never
  rebinds or extends an existing grant; a new explicit grant is required.
* **Expiry is evaluated at query time** (`expires_at <= now`), so a grant is
  invalid the instant it expires with **no cleanup job**.

### Writes, idempotency and concurrency

Each write carries a client **`event_id`** (idempotency key) and an
**`expected_version`** (optimistic concurrency token):

* identical `event_id` + identical payload → the **original result is returned**
  (`replayed: true`);
* same `event_id`, different payload → **409 Conflict**;
* `expected_version` not equal to the current state version → **409 Conflict**
  (concurrent writers serialize on the state row; exactly one wins each slot);
* any failure rolls back, so a **failed event never enters the ledger**;
* `expires_at` is normalized to UTC before hashing — the same instant in
  different timezone offsets is the same request.

A withdrawn grant cannot be resurrected by a delayed retry of the old grant id
(that is just a replay); only a **new explicit grant** restores consent.

### Rebuild

`POST /admin/rebuild` discards materialized states and refolds the ledger
inside one transaction that takes an `EXCLUSIVE` lock on `consent_events`, so
concurrent inserts block until the rebuild commits and are then present — **no
event is lost or double-applied**. The pure fold lives in `app/replay.py` and
is identical to the incremental update rule in `app/service.py`.

### Batch import

`POST /admin/import` accepts **at most 500 events**, applied in a single
transaction — **any error rolls back the entire batch**. Re-posting the exact
same batch returns the original results (`replayed: true`) without duplicating
rows.

### Subject erasure

`POST /subjects/{key}/erase`:

* deletes `subject_exports` copies and derived `consent_states`;
* anonymizes the `subjects` row (key nulled, `erased` flag + tombstone in
  `subject_deletions`);
* preserves the ledger but nulls the only identifying column
  (`subject_key_snapshot`) — the only UPDATE the ledger trigger permits;
* the audit trail keeps **operational facts only** (action, outcome, event/
  version identifiers) and never personal fields.

After erasure the subject's verify/history/export endpoints return **404**, and
a subsequent rebuild skips the tombstoned subject so its state is not
resurrected. A fresh subject with the same key can be created afterwards.

### Access control

* Bearer API keys with role **admin** (read/write) or **auditor** (read-only) —
  auditors receive 403 on any write, including publish/import/rebuild/erase.
* Every key is scoped to one organization; cross-organization reads/writes
  return 404 (existence is not revealed across orgs).

---

## HTTP API

| Method & path | Role | Purpose |
|---|---|---|
| `POST /policies` | admin | Publish a new immutable policy version |
| `GET /policies` | admin, auditor | List policy versions |
| `POST /consents` | admin | Grant or withdraw (idempotent, optimistic) |
| `POST /admin/import` | admin | Batch import up to 500 events (atomic) |
| `GET /subjects/{key}/consents/{purpose}` | admin, auditor | Current validity + basis event & policy version |
| `GET /subjects/{key}/events[?purpose=]` | admin, auditor | Immutable event history for a subject |
| `POST /admin/rebuild` | admin | Rebuild derived state from the ledger |
| `POST /subjects/{key}/erase` | admin | Erase personal data & export copies |
| `POST /subjects/{key}/exports` | admin | Create a subject data-export copy |
| `GET /subjects/{key}/exports` | admin, auditor | List a subject's export copies |
| `GET /audit[?outcome=&limit=&offset=]` | admin, auditor | Personal-data-free audit log |
| `GET /health` | — | Liveness |

### Grant payload

```json
{
  "event_id": "evt_123",
  "subject_key": "user-1001",
  "purpose": "analytics",
  "action": "grant",
  "expected_version": 0,
  "expires_at": "2030-01-01T00:00:00+00:00",
  "policy_version": 2
}
```

`action` is `grant` or `withdraw`; `policy_version` may be omitted on grants to
pin the latest version (a grant with no published policy is rejected).

### Verify response

```json
{
  "subject_key": "user-1001",
  "purpose": "analytics",
  "valid": true,
  "status": "granted",
  "reason": "grant is current",
  "state_version": 3,
  "basis_event_id": "evt_123",
  "policy_version": 2,
  "expires_at": "2030-01-01T00:00:00+00:00",
  "evaluated_at": "2026-09-20T07:52:01.081299Z"
}
```

`status` is `granted` / `withdrawn` / `no_record`; `reason` distinguishes
`grant has expired` from `consent was withdrawn`.

---

## Project layout

```
app/
  main.py          FastAPI app + error mapping
  config.py        env-based settings (CV_* prefix)
  db.py            engine/session
  models.py        ORM: ledger, states, policies, subjects, exports, audit
  schemas.py       Pydantic models
  errors.py        404/409/422 domain errors
  service.py       idempotent + optimistic write path, query-time evaluation
  replay.py        pure ledger fold + transactional rebuild
  privacy.py       erasure, exports, audit recording
  security.py      bearer API keys, roles, org scoping
  bootstrap.py     idempotent demo seed
alembic/           migration incl. DB-level immutability triggers
tests/             pytest suite (PostgreSQL)
scripts/demo.sh    guided HTTP walkthrough
```

## Test coverage

`pytest` (28 tests) covers:

* **concurrent** grant/withdraw — exactly one writer wins each version slot;
* **expiry boundary** — invalid at exactly `expires_at`, no cleanup job;
* **policy updates** — DB-enforced immutability, monotonic versions, no implicit
  rebinding, grant-before-policy rejected;
* **duplicate import** — identical batch replays originals, divergence
  conflicts, batch >500 / any-error rollback;
* **history rebuild** — matches incremental view, repairs a wiped
  materialization, and loses no events under rebuild contention;
* **post-erasure queries** — 404, personal data & exports purged, ledger
  anonymized, rebuild does not resurrect, audit stays personal-data-free;
* idempotency (incl. timezone-equivalent replay and withdrawn-not-resurrected),
  RBAC and organization isolation.
