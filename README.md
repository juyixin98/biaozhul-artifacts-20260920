# Layer-Reference Garbage Collector

A local, content-addressable container image registry (Go + HTTP + PostgreSQL)
with a **safe, auditable garbage collector** for shared layer blobs.

It demonstrates one specific hard problem: deleting unreferenced layers
*without* ever deleting a layer that is referenced by a manifest/tag or being
actively pulled — even when that reference appears **while the GC is
running**.

---

## 1. What it guarantees

| Guarantee | How |
|---|---|
| Shared layers are never collected while any image uses them | Reachability is `manifest → manifest_refs → blobs`; a blob referenced by **any** stored manifest is a GC root. |
| A pull in progress is never truncated | A blob `GET` acquires a row-level **read lease** (TTL-backed) *before* the first byte is streamed; GC re-checks and respects active leases. |
| A tag/manifest that lands during GC is not deleted | Mark uses one consistent snapshot; **each candidate is re-verified right before deletion** under a row lock that blocks concurrent referencers. |
| Uploads can't corrupt the store | Bytes always stage to a temp file first; the SHA-256 digest is verified **before** anything enters the content-addressable store. A wrong digest is rejected and the temp file removed. |
| Orphan temp files are handled separately | A dedicated maintenance pass reconciles temp files ↔ bookkeeping and audits each one; it never touches layer-GC accounting. |
| A crashed GC is recoverable | Runs left `marking`/`sweeping` by a dead process are marked `crashed`; deletion is keyed by primary key and is idempotent, so a later run never double-deletes. |
| Every decision is auditable | Each run records, per blob: `retain`/`delete`, the exact reason, size, whether it was marked at the snapshot, and the snapshot `xmin`. |

### The mark/sweep safety argument

* **Mark** — one `SERIALIZABLE READ ONLY` snapshot transaction computes the
  reachable set and records `pg_snapshot_xmin`, so the audit trail names the
  exact database state used. Nothing is deleted here.
* **Sweep** — each candidate gets its own short `READ COMMITTED` transaction:
  1. `SELECT … FROM blobs WHERE digest = $1 FOR UPDATE` — take the row lock
     first and hold it to commit.
  2. delete expired leases, then read the **current committed** reference and
     lease state.
  3. delete the row only if it is still unreferenced **and** has no active
     lease; only then unlink the file.

  A tag/manifest writer or pull that committed *before* the lock is visible in
  step 2. One that starts *after* blocks on the `FOR UPDATE` (writers take
  `FOR SHARE` on the blob; a lease `INSERT`'s foreign-key check takes one too),
  so it either waits for our commit and then fails the foreign key if we
  deleted the row, or got there first — in which case step 2 sees it and we
  retain. The small residual race (a lease inserted in the lock gap) surfaces
  as SQLSTATE `23503`/`23505`/`40001`, which the sweeper treats as retriable
  and re-verifies (backing off, max 8 attempts, then conservatively retains).

**Result: a layer referenced during scanning cannot be deleted.**

---

## 2. Layout

```
.
├── cmd/server/main.go            HTTP server entrypoint
├── internal/
│   ├── digest/                   real SHA-256 content digests (crypto/sha256)
│   ├── storage/                  on-disk CAS + temp staging (fsync, atomic publish)
│   ├── db/                       embedded SQL migrations, pgx pool
│   ├── registry/                 blobs, manifests, tags, read leases (tx core)
│   ├── gc/                       mark/sweep collector + audit + crash recovery
│   ├── maintenance/              orphan-temp reconciliation (separate audit)
│   └── httpapi/                  OCI-shaped registry API + admin API
├── internal/db/migrations/001_init.sql
├── internal/registry/*_test.go   9 scenario tests + concurrency stress test
├── internal/digest,storage/*_test.go  unit tests
├── scripts/demo.sh               end-to-end acceptance walkthrough
├── examples/manifest.template.json
└── Makefile
```

Content store layout:

```
data/
├── blobs/sha256/<ab>/<sha256:…>   published immutable blobs (sharded)
└── tmp/
    ├── uploads/<upload-id>         staged, unverified bytes
    └── trash/                      reserved for unlink-into-trash
```

---

## 3. Requirements

* Go ≥ 1.22
* PostgreSQL ≥ 13 (developed and tested on 16)
* `curl`, `python3` for the demo script

Dependencies are locked in `go.mod` / `go.sum`; the only runtime dependency is
the pgx driver (`github.com/jackc/pgx/v5 v5.7.2`).

---

## 4. Local startup

### 4.1 Database

```bash
make db
# equivalent to:
#   sudo -u postgres psql -c "CREATE ROLE gcuser LOGIN PASSWORD 'gcpass';"
#   sudo -u postgres psql -c "CREATE DATABASE registry_gc OWNER gcuser;"
#   sudo -u postgres psql -c "CREATE DATABASE registry_gc_test OWNER gcuser;"
```

### 4.2 Run the server

```bash
make run
# or: go run ./cmd/server
# flags (also overridable via env):
#   --addr        127.0.0.1:8080        ($ADDR)
#   --dsn         postgres://gcuser:gcpass@127.0.0.1:5432/registry_gc?sslmode=disable
#   --data-dir    ./data                ($DATA_DIR)
#   --lease-ttl   1m
```

Schema migrations run automatically on boot (idempotent `IF NOT EXISTS`).

### 4.3 Run the acceptance demo

```bash
make run          # in one terminal
make demo         # in another
```

The script pushes two images sharing a base layer, shows a digest-mismatch
rejection, runs GC, deletes one image, runs GC again and prints the per-blob
retain/delete reasons.

---

## 5. HTTP API

### Registry (OCI Distribution-shaped, minimal)

| Method & path | Purpose |
|---|---|
| `POST /v2/<name>/blobs/uploads/` | start a staged upload → `Location`, `Docker-Upload-Uuid` |
| `PATCH /v2/<name>/blobs/uploads/<id>` | stream a chunk into the temp file |
| `PUT /v2/<name>/blobs/uploads/<id>?digest=<d>` | verify SHA-256, then publish into the CAS |
| `DELETE /v2/<name>/blobs/uploads/<id>` | abort an upload |
| `HEAD /v2/<name>/blobs/<digest>` | existence + size |
| `GET /v2/<name>/blobs/<digest>` | download; acquires a lease first (`X-Lease-Ids`) |
| `PUT /v2/<name>/manifests/<ref>` | store a manifest (digest) **and tag it** (name) |
| `GET /v2/<name>/manifests/<ref>` | fetch manifest; pins referenced layers for the pull |
| `DELETE /v2/<name>/manifests/<tag>` | untag |
| `DELETE /v2/<name>/manifests/<digest>` | delete the manifest and its reference edges |

### Administration

| Method & path | Purpose |
|---|---|
| `POST /admin/gc/run` | run GC now; returns the full auditable report |
| `POST /admin/gc/run?pause_after_mark=1&pause_token=<t>` | pause between mark and sweep (deterministic race tests) |
| `POST /admin/gc/resume?token=<t>` | release the pause |
| `GET /admin/gc/report?id=<n>` | fetch a persisted run + per-item audit rows |
| `POST /admin/orphans/run[?dry_run=1]` | reconcile orphan temp files (separate audit tables) |

### Manual blob push/pull

```bash
# push
content="hello-layer"
dgst=$(printf '%s' "$content" | sha256sum | awk '{print "sha256:"$1}')
loc=$(curl -s -X POST localhost:8080/v2/demo/blobs/uploads/ -D - -o /dev/null | awk '/^[Ll]ocation:/{print $2}' | tr -d '\r')
curl -s -X PATCH "localhost:8080$loc" --data-binary "$content" -o /dev/null
curl -s -X PUT "localhost:8080$loc?digest=$dgst" -D - -o /dev/null   # 201

# pull (X-Lease-Ids shows the read lease)
curl -sD - "localhost:8080/v2/demo/blobs/$dgst" -o /tmp/layer -H 'X-' 2>/dev/null | grep -i lease

# GC
curl -s -X POST localhost:8080/admin/gc/run | python3 -m json.tool
```

---

## 6. Tests

```bash
make test          # all packages (needs registry_gc_test database)
make test-race     # with the race detector + 3s concurrent stress
```

The database used by tests is `registry_gc_test` (override with
`TEST_DATABASE_URL`). Each test creates its own isolated Postgres schema, so
they run independently.

### Covered scenarios

| Test | What it proves |
|---|---|
| `TestSharedLayerIsRetained` | Two images sharing a layer; deleting one image removes only its private layer + config; the shared layer survives, with retain/delete reasons. |
| `TestPullDeleteRace_Lease` | A candidate with an active read lease is retained; after the lease is released the next GC deletes it. |
| `TestPullDeleteRace_HTTP` | A **real** in-flight HTTP `GET` holds its lease; concurrent GC retains the blob. |
| `TestPullArrivesDuringMarkSweepPause` | GC pauses after mark; a real pull starts in the gap; the resumed sweep sees the new lease and retains. |
| `TestNewTagAfterMarkIsRechecked` | A new manifest/tag referencing the candidate lands after the snapshot; sweep-time re-verification retains it (the core "referenced during scanning" case). |
| `TestGCCrashRecovery` | A run stuck `sweeping` is marked `crashed`; the next run completes, never double-deletes, and a re-run is a clean no-op. |
| `TestDigestVerificationRejectsBadContent` | Wrong claimed digest → 400, no blob published, temp file + bookkeeping removed; correct digest then works. |
| `TestOrphanTempFilesCleaned` | Orphan temp files and dangling bookkeeping rows are reconciled with distinct reasons in separate audit tables. |
| `TestUnreferencedBlobCollected` | An uploaded-but-never-referenced blob is collected once, audited end-to-end. |
| `TestConcurrentGCStress` | 3 seconds of GC running concurrently with publishers, 4 HTTP pullers and transient uploads; continuously-referenced layers survive intact. |
| `internal/digest`, `internal/storage` tests | NIST SHA-256 vectors; stage→verify→publish; tamper rejection; idempotent publish; CAS layout. |

All hashing is real `crypto/sha256` (no stubs/mocks); digest tests assert the
published FIPS 180-2 vectors for `""` and `"abc"`.

### Auditable report shape

```json
{
  "run_id": 7,
  "status": "completed",
  "snapshot_xmin": 300614,
  "marked_count": 3,
  "candidate_count": 2,
  "deleted_count": 2,
  "retained_count": 0,
  "recovered_previous_crashed_run": false,
  "items": [
    {
      "blob_digest": "sha256:8e69…",
      "decision": "delete",
      "reason": "deleted: unreferenced at mark snapshot and still unreferenced with no active lease at sweep time",
      "size_bytes": 18,
      "marked_at_snapshot": false
    },
    {
      "blob_digest": "sha256:ab12…",
      "decision": "retain",
      "reason": "retained on re-check: newly referenced by a manifest after mark snapshot",
      "size_bytes": 25,
      "marked_at_snapshot": false
    }
  ]
}
```

Retention-reason vocabulary: reachable at snapshot; new manifest reference
after snapshot; active read lease; reference **and** lease; persistent
conflict (conservative retain). Delete always states that the blob was
unreferenced at the snapshot and remained so with no active lease at sweep.

---

## 7. Design notes & scope

* **Roots.** A blob is live while any stored manifest references it (layer or
  config descriptor). Tags name manifests; deleting a tag alone does not make
  layers garbage — the manifest must also be deleted (`DELETE …/manifests/<digest>`).
  This matches the standard two-phase registry-GC model.
* **Config blobs** are treated exactly like layers for reachability so a
  manifest never points at a deleted config; they are labelled `kind='config'`
  in `manifest_refs`.
* **Lease TTL** bounds how long a crashed reader can pin a blob; normal pulls
  release their lease immediately after the body is copied.
* **DB-then-file ordering.** The blob row is deleted first, then the file is
  unlinked. If the unlink fails the reason is recorded honestly
  (`FILE UNLINK FAILED`) and the orphan reconciler will report the stranded
  CAS file on a later pass.
* Pure backend: there is deliberately no UI.
