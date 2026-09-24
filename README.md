# Build Cache Key Audit (buildcache)

A small, **backend-only** local build-cache service written in Go with an HTTP
API and SQLite storage. Every cache key is computed — never guessed — from a
canonical description of a build, and every cached artifact is verified by
digest before it is served. Corrupt entries are physically isolated, and
concurrent builds of the same key collapse to a single real compilation.

No front end, no external services, no CGO. All hashing, process execution,
and persistence below is real.

---

## 1. What the cache key binds

A cache key is `bck1-<sha256(canonical JSON)>` over a single versioned
document containing:

| Component       | What is captured |
|-----------------|------------------|
| **Source manifest** | Every declared file: project-relative path, size, and SHA-256 of its bytes. Paths are cleaned, containment-checked, and **sorted**. |
| **Toolchain summary** | Toolchain name, the exact version command, its **real captured stdout**, the resolved executable path, and the **SHA-256 of that executable**. |
| **Arguments** | The ordered argument vector (order is significant). |
| **Command** | The full command template, tokenized (never run through a shell). |
| **Target platform** | Canonicalized key/value pairs, e.g. `GOOS`, `GOARCH`, sorted. |
| **Declared environment** | Only variables the request explicitly declares, sorted by name. The server's own ambient environment is never read into a key. |

The envelope carries an explicit `"version": "bck1"` so a change to the
canonicalization rules can never be misread as an old rule.

### Missing file vs. empty file

These are deliberately **different**:

* a present, zero-byte file records `present:true`, size `0`,
  digest `e3b0c4…b855` (the SHA-256 of the empty input);
* a missing file records `present:false`, empty digest, zero size.

So adding a previously-absent file — even an empty one — changes the key.
Required files that are missing fail the request; files marked `optional` are
recorded as absent.

---

## 2. Correctness guarantees

* **Verified hits.** A `succeeded` entry is served only after recomputing the
  blob's SHA-256 and size and matching the stored values. The cache never
  hands back bytes it has not just verified.
* **Corruption isolation.** If the blob is missing, truncated, or altered,
  the file is **moved into `data/quarantine/`**, the row is marked
  `quarantined`, an event with expected vs. observed digest is written, and
  the request transparently rebuilds. The artifact endpoint returns
  `410 Gone` with the reason instead of bad bytes.
* **Single publisher per key.** Claiming a key is one SQLite transaction.
  Concurrent identical builds: exactly one runs the compiler; the others wait
  on the lease and take the verified hit. A waiter that outlives its deadline
  receives `409 Conflict`.
* **Failures are never cached as success.** A non-zero exit, a process that
  never ran/timed out, **or an exit-0 run that fails to produce the declared
  artifact** all publish `failed` with no artifact fields. A failed entry is
  never a hit and is rebuilt on the next request.
* **Restart recovery.** At boot, rows left in `building` by a crashed process
  are marked `interrupted` and become claimable again; `succeeded` rows
  survive and remain verified hits.
* **No ambient leakage.** Child builds run under a small fixed base
  environment (`PATH`, `HOME`, `LANG`, Go toolchain dirs) plus exactly the
  declared variables and target — nothing else from the server.
* **Content-addressable storage.** Blobs are stored as
  `data/blobs/<sha[:2]>/<sha>`, renamed from the build's work dir and
  re-hashed after the move before the success row is committed.

---

## 3. Layout

```
cmd/buildcache/          HTTP server entrypoint
internal/key/            canonical material + SHA-256 key
internal/store/          SQLite: entries, leases, quarantine events
internal/builder/        source resolution, real tool execution, verification
internal/server/         HTTP handlers
examples/workspace/      real Go project used by the demo (build-tag flavors)
examples/*.json          sample requests
scripts/demo.sh          end-to-end acceptance script (starts/stops server)
```

SQLite is accessed through the pure-Go `modernc.org/sqlite` driver, so the
project builds with `CGO_ENABLED=0`.

---

## 4. Build & run locally

Requirements: **Go 1.22+**, `sh`/`curl`/`python3` for the demo script.

```bash
# from the repository root
go build ./...
go run ./cmd/buildcache \
  -addr 127.0.0.1:8080 \
  -data ./data \
  -source-root ./examples/workspace
```

Flags:

* `-addr`     listen address (default `127.0.0.1:8080`)
* `-data`     directory for `cache.db`, `blobs/`, `quarantine/`
* `-source-root` root used when sources are referenced by path (inline
  sources do not need it)

Health check:

```bash
curl -s http://127.0.0.1:8080/healthz
```

### Dependencies are locked

`go.mod` / `go.sum` pin `modernc.org/sqlite v1.34.1` and its transitive
modules; `go mod verify` confirms them. Nothing is fetched at run time, and
child builds run with `GOPROXY=off`.

---

## 5. HTTP API

All bodies are JSON. Unknown fields are rejected.

### `POST /audit/key`
Compute the key and canonical material **without building**.

### `POST /builds`
Return a verified hit, or run the build as the key's single publisher.
* `201 Created` — a fresh build ran (`hit:false`)
* `200 OK` — a verified hit (`hit:true`)
* `409 Conflict` — another publisher owns the key and `wait_seconds` elapsed
* `400` — invalid request

### `GET /builds/{key}`
Stored entry: status, attempt, exit code, stdout/stderr, artifact digest.

### `GET /builds/{key}/artifact`
Stream the verified artifact (`X-Artifact-SHA256` header), or `410 Gone` with
a quarantine reason, or `409` for a non-succeeded entry.

### `GET /builds/{key}/quarantine`
List isolation events (expected vs. observed digest, reason, timestamp).

### Request fields

```json
{
  "toolchain": "go",
  "version_command": ["go", "version"],
  "command": ["go", "build", "-buildvcs=false", "{args}", "-o", "{artifact}", "."],
  "args": [],
  "target": {"GOOS": "linux", "GOARCH": "amd64"},
  "env": {"GOFLAGS": "-tags=pro"},
  "sources": [
    {"path": "main.go"},
    {"path": "notes.txt", "inline_b64": "aGVsbG8="},
    {"path": "maybe.cfg", "optional": true}
  ],
  "artifact": "app",
  "timeout_seconds": 120,
  "wait_seconds": 120
}
```

* `command` is an argv array with three optional tokens: `{sources}` expands
  to the sorted manifest paths, `{args}` to the argument vector, `{artifact}`
  to the declared output path. No shell interpolation occurs.
* Sources are either read under `-source-root` by relative path, or supplied
  inline via standard/URL-safe base64. An **explicit `"inline_b64": ""`**
  means a present empty file; omitting the field means "read from disk".
* `env` lists **declared** variables only; it is part of the key.

---

## 6. One-command acceptance

The script builds the server, picks a free local port, and runs 32 checks
against a throwaway data directory — including a **real Go compilation**:

```bash
./scripts/demo.sh
```

It verifies, end to end:

1. health;
2. audit keys differ when a declared env var changes;
3. a cold real compile misses and the produced binary runs as
   `buildcache demo: flavor=lite`;
4. the identical request is a verified hit;
5. `GOFLAGS=-tags=pro` misses and produces a genuinely different
   `… flavor=pro` binary;
6. omitting the declared env can never reuse the stale `pro` result;
7. a failing build (exit 8) is recorded `failed` and serves no artifact;
8. five simultaneous same-key builds → exactly **one** `201` publisher and
   four `200` hits over one identical blob;
9. a tampered artifact is quarantined (`hash_mismatch`), logged, then healed
   by a rebuild;
10. a deleted artifact is quarantined (`artifact_missing`) and healed;
11. a server restart preserves verified hits and recovers leases;
12. a missing file and a present empty file produce different keys.

Final line on success: `RESULT: 32 passed, 0 failed`.

### Try it by hand

```bash
go run ./cmd/buildcache -source-root ./examples/workspace &

# inspect the key without building
curl -s -d @examples/request-go-pro.json -H 'Content-Type: application/json' \
  http://127.0.0.1:8080/audit/key | head -40

# cold real compile (note http_code 201), then hit (200)
curl -s -w '\n%{http_code}\n' -d @examples/request-go-lite.json \
  -H 'Content-Type: application/json' http://127.0.0.1:8080/builds
curl -s -w '\n%{http_code}\n' -d @examples/request-go-lite.json \
  -H 'Content-Type: application/json' http://127.0.0.1:8080/builds

# failed build is reported honestly
curl -s -d @examples/request-fail.json -H 'Content-Type: application/json' \
  http://127.0.0.1:8080/builds
```

---

## 7. Tests

```bash
go test -race -count=1 ./...
```

Coverage by package:

* `internal/key` — determinism/order-insensitivity, every key component is
  bound, missing-vs-empty, path traversal rejection, canonical JSON shape;
* `internal/store` — single-publisher claim, contention (50 goroutines, one
  winner), failure never carries artifact fields, restart lease recovery,
  quarantine, not-found;
* `internal/builder` — a **real `go build`** miss/hit/invalidation across
  source and env changes, omitted-env cannot reuse a stale result,
  concurrent single publisher, tamper **and** loss quarantine + heal,
  failed/exit-0-no-artifact never cached, restart, missing-vs-empty;
* `internal/server` — status codes, artifact download/digest header,
  tamper/missing → 410 + quarantine event, audit-key behavior, validation,
  honest failure reporting.

If a behavior cannot hold, the code reports it plainly: build failures carry
the real exit code and stderr, a post-store verification failure is surfaced
as `failed`, and an unverifiable download is never returned as success.
