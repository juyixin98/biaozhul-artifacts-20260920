# Resumable DAG Executor (Go, net/http only)

A local backend-only service that executes a directed acyclic graph (DAG) of
**whitelisted pure functions**, with:

- **Dependencies** — a node starts only after every dependency succeeded;
  dependency results are passed along and can be referenced with `{"$ref": "..."}`.
- **Finite retries** — per-node or DAG-wide retry count with fixed backoff.
- **Cancellation** — `POST /api/dags/{id}/cancel`; succeeded nodes keep their
  cached results, no new downstream node is ever started, in-flight tasks are
  interrupted via `context.Context`.
- **Result caching & durable scheduling state** — every state transition is
  fsynced to disk (atomic temp-file + rename) *before* it takes effect, so a
  confirmed-succeeded node **never executes twice**, even across process
  restarts/crashes.
- **Validation before execution** — dependency cycles and missing
  dependencies are rejected with HTTP 400 at submit time.

No UI. Standard library only (`net/http`, `encoding/json`, …): **zero
third-party dependencies**, so `go.mod` alone fully pins the build.

## Requirements

- Go **1.23+** (developed and tested with go1.23.4 linux/amd64)
- No other dependencies. `jq` and `curl` are only needed to run the demo
  script; the HTTP API itself needs nothing.

## Build & run

```bash
go build -o dagserver ./cmd/dagserver
./dagserver -addr :8080 -data ./data
```

Flags:

| flag     | default  | meaning                              |
|----------|----------|--------------------------------------|
| `-addr`  | `:8080`  | listen address                       |
| `-data`  | `./data` | directory for snapshot JSON files    |

On startup the service lists every snapshot in `-data`, loads it, and resumes
non-terminal DAGs. Nodes that were `running` when a previous process died have
no confirmed result and are rolled back to `pending` and retried; nodes that
were `succeeded` are reused as-is.

SIGINT/SIGTERM trigger a graceful shutdown: in-flight tasks get a cancelled
context and are rolled back to `pending` on disk, so restarting continues them
exactly once more (never double-commits).

## Tests

```bash
go test ./...            # all packages
go test -race ./...      # with the race detector (used during development)
go test -v -run TestRestartSkipsSucceededAndRetriesInflight ./internal/engine
```

## HTTP API

All bodies are JSON.

### `POST /api/dags` — create & start a DAG

```json
{
  "name": "diamond",
  "retries": 0,
  "backoff_ms": 0,
  "nodes": [
    {"id": "top", "type": "const", "params": {"value": 10}},
    {"id": "left", "type": "mul", "deps": ["top"],
     "params": {"x": {"$ref": "top"}, "y": 2}},
    {"id": "right", "type": "add", "deps": ["top"],
     "params": {"x": {"$ref": "top"}, "y": 5}},
    {"id": "bottom", "type": "sum_deps", "deps": ["left", "right"]}
  ]
}
```

- `retries` / `backoff_ms` are DAG-level defaults (retries = attempts *after*
  the first one; `retries: 2` means at most 3 attempts). Both can be
  overridden per node with the same field names.
- `deps` lists node IDs that must succeed first.
- Any parameter value may be `{"$ref": "<depNodeID>"}` (also nested inside
  arrays/maps); it is replaced with that dependency's cached result.
- Returns `201` with the initial snapshot, including the generated `id`.
- Returns `400` for: cycle, missing/duplicate dependency, unknown task type,
  duplicate/invalid node ID, bad retry bounds, malformed JSON, unknown fields.

### `GET /api/dags/{id}` — current state (results cache included)

Node states: `pending | running | retrying | succeeded | failed | cancelled |
skipped`. DAG states: `running | succeeded | failed | cancelled`.

### `POST /api/dags/{id}/cancel` — cancel

Idempotent. Returns the post-cancel snapshot. Nodes that had not succeeded
become `cancelled`; a running node that finishes first keeps `succeeded`.

### `GET /api/dags` — list all DAGs
### `GET /api/tasks` — whitelisted function names
### `GET /healthz` — liveness

## Whitelisted functions

Only these built-in, pure functions can be referenced by `type` (see
`internal/task/registry.go`):

| type       | params                                   | result                              |
|------------|------------------------------------------|-------------------------------------|
| `const`    | `value` (any JSON)                       | the value                           |
| `add`      | `x`, `y` numbers                         | `x + y`                             |
| `mul`      | `x`, `y` numbers                         | `x * y`                             |
| `div`      | `x`, `y` numbers                         | `x / y`, error on divisor 0         |
| `sum_deps` | —                                        | sum of all dependency results       |
| `concat`   | `values` array of strings, `sep` string  | joined string                       |
| `length`   | `value` string or array                  | length (runes for strings)          |
| `fail`     | optional `message`                       | always errors (for demos/tests)     |

Adding a function means writing a pure `func(context.Context, map[string]any,
map[string]any) (any, error)` and registering it in `task.Builtins()`; user
request bodies can never cause arbitrary code to run.

## Request samples & demo

`examples/` contains ready requests:

- `diamond.json` — classic diamond: `top=10 → left=*2(20), right=+5(15) →
  bottom=sum(35)`
- `middle_failure.json` — middle node fails all 3 attempts (1 + 2 retries);
  the DAG fails and the downstream node is `skipped` without running
- `cancel_demo.json` — a node stuck in a long retry/backoff loop; cancel it
  and observe `downstream` stay at `attempts: 0`
- `cycle.json`, `missing_dep.json` — validation rejections

```bash
./examples/demo.sh                            # against :8080
BASE=http://localhost:18080 ./examples/demo.sh
```

Manual calls:

```bash
# submit
curl -s -X POST localhost:8080/api/dags \
  -H 'Content-Type: application/json' --data-binary @examples/diamond.json

# poll
curl -s localhost:8080/api/dags/<id> | jq .

# cancel
curl -s -X POST localhost:8080/api/dags/<id>/cancel | jq .
```

## How durability & exactly-once-success work

1. On submit the full initial snapshot is written to `<id>.json`.
2. Before a node is launched, its transition to `running` (with attempt
   number) is persisted.
3. The function runs. Its `succeeded` + result (or `retrying`/`failed`) is
   persisted before anything downstream can be scheduled.
4. Writes go to a `.tmp` file followed by `os.Rename` (atomic replace), so a
   crash leaves either the previous complete snapshot or the new one.
5. Restart: `running` → `pending` (outcome unconfirmed → safe to retry);
   `succeeded` is untouched and reused. Hence the guarantee is **at-least-once
   execution, at-most-once successful commit** — a confirmed node result is
   never recomputed.

Consequences worth knowing:

- A task that side-effected externally *and* crashed before its success was
  durable would be re-run — built-in tasks are pure, so this is invisible;
  register only idempotent/pure functions.
- Persistence is a single JSON file per DAG (designed for local use; up to a
  few hundred nodes per DAG). The `engine.Store` interface can be swapped for
  a transactional database without touching scheduler logic.

## Layout

```
cmd/dagserver/main.go      HTTP service entrypoint, flags, graceful shutdown
internal/model/            wire + persisted types
internal/task/             whitelist registry, $ref resolution, builtins
internal/engine/           scheduler: validation, cycle detection, retries,
                           cancel, persistence protocol, crash recovery
internal/store/            atomic JSON file store
examples/                  sample request bodies + demo.sh
```

## Verified acceptance criteria (2026-09-24, linux/amd64, go1.23.4)

- **Diamond + middle failure**: observed `bottom=35`; failing middle node
  tried exactly 3 attempts (1 + 2 retries), DAG `failed`, downstream `skipped`
  with `attempts: 0`.
- **Restart**: SIGTERM then relaunch over the same `-data` dir reloads both
  DAGs and replays nothing; the in-flight-crash variant is covered by
  `TestRestartSkipsSucceededAndRetriesInflight` (succeeded node executes
  exactly once across the simulated crash; in-flight node rolls back and runs
  once more).
- **Cancel**: after cancel the retrying node stopped (`cancelled`, no further
  attempts) and `downstream` remained `cancelled, attempts: 0`; still true one
  second later.
- **Cycle / missing dep**: both rejected at submit time with HTTP 400.
- `go test -race ./...` green for `engine`, `server`, `store`, `task`.

## Not implemented (out of scope or deliberately omitted)

- No UI / dashboard (backend only, as requested).
- No authentication/TLS (local service; put a reverse proxy in front if
  needed).
- No task timeout parameter per node (only the HTTP server lifecycle + cancel
  bound execution); a node that ignores its context can delay graceful
  shutdown.
- Scheduler ticking uses a 10 ms poll timer plus event wakeups; this is
  intentionally simple rather than a timing wheel.
- Files are the only store backend (interface exists; no SQL driver added to
  preserve the zero-dependency guarantee).
