# testmerge — multi-shard test-result merging service (backend only)

A local Go service that merges test-execution events from multiple shards
into one repeatable summary. It is deliberately a **backend-only,
local-only** service: there is no UI and no cloud connection. It runs only
the fixture commands a user explicitly supplies, stores its durable event
log in a cache directory that is separate from fixture working directories,
and never treats a missing result as a pass.

## What it guarantees

1. **Arrival-order independence (repeatable summaries).**
   Events are reduced in canonical order `(seq_no, event_id)`, not in the
   order they happen to arrive. Replaying the same multiset of events —
   shuffled, duplicated, or after a process restart — always yields an
   identical summary.

2. **Attempt id ≠ test id; retries do not clobber history.**
   A test (`test_id`) can have many attempts (`attempt_id`, ordered by
   `attempt_no`). A test's outcome is taken from its **latest attempt only**;
   older attempts and their results remain visible.

3. **Late results never overwrite a newer attempt.**
   A terminal result that arrives late for an *old* attempt is recorded as a
   `late_write` against that attempt but its first terminal status is kept;
   it can never change the outcome the newer attempt already established.
   Duplicate `event_id`s are applied exactly once.

4. **Four explicit statuses; missing ≠ passed.**
   Tests and runs are one of `passed | failed | cancelled | incomplete`.
   - an attempt that started but never finished → `incomplete`
   - a test declared at run creation but never observed → `incomplete`
   - a run whose `run_finished` was lost (executor crash) → `incomplete`,
     even if every observed result passed
   - an empty run → `incomplete`

5. **Executor crash & retry are first-class.**
   A crashing fixture's partial events are retained; re-running the same
   `execution_id` is idempotent; a later retry can supply the missing
   results and `run_finished`.

6. **Only explicit fixture commands run; cache and work dirs are separate.**
   `argv[0]` must resolve (symlinks included) to a regular executable inside
   `-fixtures-root`; `../` escapes and absolute paths are rejected. Commands
   run via direct `execve` (no shell), each in its own directory under
   `-work-dir`. Durable JSONL logs live under `-cache-dir`.

## Layout

```
cmd/testmerge/          HTTP server entrypoint
internal/domain/        status constants + Event shape
internal/merge/         pure reduction engine (validate, Reduce, Summarize)
internal/store/         append-only JSONL event store (cache dir)
internal/executor/      constrained fixture runner (work dir, no shell)
internal/api/           JSON HTTP handlers
testdata/fixtures/      the only runnable commands (gen_events.sh, ...)
scripts/demo.sh         end-to-end acceptance walkthrough
docs/API.md             full HTTP API reference with request samples
```

## Event model

One JSON object per event (see `internal/domain/event.go`):

| type | required fields |
|------|-----------------|
| `run_started` | emitted by the server at run creation; optional `tests: [...]` declares expected test ids |
| `run_finished` | `seq_no`, `status` (`passed\|failed\|cancelled\|incomplete`), optional `cancelled` |
| `shard_started` / `shard_finished` | `seq_no`, `shard` |
| `attempt_started` | `seq_no`, `shard`, `test_id`, `attempt_id`, `attempt_no` |
| `attempt_finished` | the above plus `status` (`passed\|failed\|cancelled`) |

- `event_id` — globally unique; replays with the same id are ignored.
- `seq_no` — executor-assigned monotonic number within the run; defines
  canonical order. A `seq_no` shared by two different events is rejected.
- `attempt_no` — 1-based attempt ordering for one test (retries increment
  it). The highest number is the latest attempt.

## Build & test

```bash
go build ./...
go test ./...          # unit + HTTP/integration tests (runs the fixtures)
go test -race ./...
```

## Run

```bash
go build -o bin/testmerge ./cmd/testmerge
./bin/testmerge \
  -addr 127.0.0.1:8099 \
  -cache-dir ./.testmerge-cache \
  -work-dir  ./.testmerge-work \
  -fixtures-root ./testdata/fixtures
```

Flags: `-addr`, `-cache-dir`, `-work-dir`, `-fixtures-root`,
`-read-timeout`. The server binds loopback by default and has no auth; do
not expose it directly.

Then run the acceptance walkthrough (requires `curl` and `jq`):

```bash
scripts/demo.sh http://127.0.0.1:8099
```

See [`docs/API.md`](docs/API.md) for request/response samples.

## Fixtures

`testdata/fixtures/gen_events.sh <scenario>` prints newline-delimited JSON
events. Scenarios:

- `happy` — two shards, 2 passed / 1 failed, clean finish
- `shuffled` — same events/ids/seq as `happy`, scrambled line order
- `duplicates` — every `happy` event twice (restart re-stream)
- `retry` — failed attempt 1, passed attempt 2
- `crash` — partial events, no `run_finished`, exit 91
- `late-write` — late result for the old attempt after a newer pass
- `cancelled` — run cancelled while a test is in flight
- `missing` — one started-unfinished test + one unseen declared test
- `malformed` — invalid stdout lines interleaved with valid events

Plus `touch_marker.sh` (proves cwd isolation), `hang.sh` (timeout) and
`echo_args.sh` (proves no shell interpretation of argv).

## How reduction works

`Reduce(runID, events)` (in `internal/merge`):

1. reject foreign-run and structurally invalid events;
2. drop duplicate `event_id`s (counted in `ReduceResult.Duplicate`);
3. sort by `(seq_no, event_id)` and reject `seq_no` collisions;
4. fold events into a `Run`;
5. `Summarize()` derives tests/shards/counts/status purely from that state.

State is never mutated by reads: every `GET /runs/{id}` re-runs the pure
reduction over the JSONL log, which is what makes summaries deterministic
and restart-safe.

## Persistence

```
<cache-dir>/runs/<runID>/events.jsonl       # one JSON event per line
<cache-dir>/runs/<runID>/executions.jsonl   # one audit record per execute
<work-dir>/<runID>/<executionID>/...        # fixture cwd, isolated per run
```

Appends are `O_APPEND` + `fsync`. Deleting a work directory never affects
recorded events and vice versa.

## Non-goals

- No frontend / dashboard.
- No network/cloud integrations, telemetry, or outbound connections.
- No authentication or multi-tenant authorization (loopback only).
- The service does not schedule tests itself; it only executes explicitly
  requested fixture argv and merges the events they emit.
