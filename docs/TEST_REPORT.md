# Test report — actually executed 2026-09-24

Environment: Linux 6.8.0-90-generic, `go version go1.22.2 linux/amd64`,
`jq` and `curl` available. Everything in this file was run against the
code in this repository; failures hit during development are recorded
together with the fix, and the final clean run is at the bottom.

## Commands and results

### 1. Build / vet / format

| command | result |
|---|---|
| `go build ./...` | **OK** (also `go build -o bin/testmerge ./cmd/testmerge`) |
| `go vet ./...` | **OK** — `VET_OK` |
| `gofmt -l .` | no files listed — `GOFMT_OK` |

### 2. Automated tests

`go test -count=1 ./...` — **all packages PASS**:

```
?   github.com/local/testmerge/cmd/testmerge   [no test files]
?   github.com/local/testmerge/internal/domain [no test files]
ok  github.com/local/testmerge/internal/api       0.132s
ok  github.com/local/testmerge/internal/executor  0.234s
ok  github.com/local/testmerge/internal/merge     0.003s
ok  github.com/local/testmerge/internal/store     0.021s
```

`go test -race -count=1 ./...` — **all packages PASS** under the race
detector (api 1.188s, executor 1.251s, merge 1.019s, store 1.036s).

Statement coverage:

```
internal/api       74.3%
internal/executor  80.9%
internal/merge     88.9%
internal/store     77.4%
```

What the tests assert (27 test functions):

- **merge engine** (`internal/merge/merge_test.go`)
  - happy-path counts;
  - **reorder invariance**: 20 random shuffles + injected duplicate events
    produce a summary identical to in-order ingestion;
  - retry: highest `attempt_no` decides the outcome;
  - **late result for an old attempt is ignored** (first terminal status
    kept, counted in `late_writes`), newer attempt's pass survives;
  - executor crash (no `run_finished`, unfinished attempt) → `incomplete`;
  - events present but no `run_started` → `incomplete`;
  - cancellation (run cancelled mid-test → unfinished test `cancelled`,
    already-passed test stays `passed`);
  - **missing results are never passed**: started-unfinished and
    declared-but-unobserved tests both `incomplete`, run `incomplete`;
  - duplicate `event_id` applied once; `seq_no` collision rejected;
    validation rejects bad status/ids/seq; foreign-run events rejected;
    orphan finish (no start) flagged; empty run `incomplete`.
- **store** (`internal/store/store_test.go`) — append+reload durability,
  duplicate rejection, seq collision not persisted, run-id path-traversal
  rejected (`../escape`, `a/b`, control chars), 404 on unknown run, sorted
  listing, synthesized finish seq = max+1, execution audit log.
- **executor** (`internal/executor/executor_test.go`) — happy/crash/malformed
  parsing, exit-91 crash keeps partial events and no `run_finished`, timeout
  kills a wedged fixture and retains pre-timeout events, per-execution work
  dir is distinct from cache/fixtures, traversal/absolute/non-executable/
  missing fixture rejected, argv passed verbatim with no shell.
- **HTTP API** (`internal/api/server_test.go`) — full scenario tests via
  httptest: shuffled execute vs ordered execute equal; double replay yields
  only duplicates; crash → 409-style dedup on re-execute → retry completes;
  late-write / missing / cancelled / duplicates / malformed fixture flows;
  manual event validation (400/409); unknown run 404; escaping command 400;
  execution audit recorded; fixtures root stays clean.

### 3. Live end-to-end run

Server started with **separate** cache and work directories:

```bash
go build -o bin/testmerge ./cmd/testmerge
./bin/testmerge -addr 127.0.0.1:8099 \
  -cache-dir ./.testmerge-cache \
  -work-dir  ./.testmerge-work \
  -fixtures-root ./testdata/fixtures
scripts/demo.sh http://127.0.0.1:8099
```

Result: **`DEMO OK`**. Key observed outputs:

| scenario | observed |
|---|---|
| shuffled vs in-order | identical `failed`, counts `{passed:2, failed:1}` — `MATCH` |
| replay ×2 | `accepted:0, duplicates:11`, status unchanged |
| executor crash | exit 91, 4 events ingested, status `incomplete`, `missing_finish:true`, `{passed:1, incomplete:1}` |
| same execution re-run | `ingested:0, duplicates:4`, counts unchanged |
| retry after crash | 2 events accepted → status `passed`, `{passed:2}` |
| late write | run `passed`, `late_writes_total:1`, latest attempt `t-2` passed, old attempt stays `failed` with `late_writes:1` |
| missing | `incomplete`, `{passed:0, incomplete:2}` (test-maybe + test-ghost) |
| cancellation | `cancelled:true`, `{passed:1, cancelled:1}` |
| invalid status / path escape | both HTTP 400, error bodies returned |
| summary read ×3 | byte-identical all three times |

**Restart durability**: the server was stopped and restarted against the
same `-cache-dir`. All 7 runs reappeared with identical statuses;
`demo-crash` was re-derived from `events.jsonl` as `passed`
(7 events applied), and `demo-miss` as `incomplete`. The execution audit
showed both crash executions (exit 91, 4 events each) with their own work
dirs. On-disk layout confirmed:

```
.testmerge-cache/runs/<id>/events.jsonl       (durable log)
.testmerge-cache/runs/<id>/executions.jsonl   (audit)
.testmerge-work/<id>/<execution-id>/          (isolated fixture cwd)
```

## Failures encountered during development (and their fixes)

These were real failures seen before the clean run above; none remain.

1. **Compile error** — summary referenced a non-existent field
   `finishSeq` (actual name `finishedSeq`). Fixed; `go build` clean.
2. **Test compile error** — compared a value type `TestSummary` to `nil`.
   Fixed the assertion.
3. **`TestNoRunStartedIsIncomplete` failed** — a run with shard/attempt
   events but no `run_started`/`run_finished` was reported `passed`.
   Root cause: `missing_finish` required `started==true`. Fixed to treat
   *any* observed activity without `run_finished` as unfinished
   (`!finished && (started || attempts>0 || shards>0)`).
4. **`TestUnknownRun404` failed (200 instead of 404)** —
   `GET /runs/{id}/executions` returned an empty list for a nonexistent
   run. Fixed `LoadExecutions` to return `ErrNotFound` when the run dir is
   absent (empty file on an existing run still returns an empty list).
5. **Timeout test took 30s** — killing the wrapper shell left `sleep`
   holding the stdout pipe, so `cmd.Run` blocked until the child exited.
   Fixed by running fixtures in their own process group
   (`Setpgid`) with a group-kill `Cancel` and `WaitDelay`; fixture
   `hang.sh` uses `exec sleep`. Executor package test time dropped from
   ~30.0s to ~0.23s.
6. **Operational (not code)** — the first chosen demo port 18099 was
   already bound by another local process (`bind: address already in
   use`, whose `/healthz` returned an unrelated `timeMillis` body).
   Re-ran on a free port; final verification used the default 8099 on a
   fresh cache.

## Known limitations / not done (truthful)

- No frontend (explicitly out of scope).
- No auth/TLS; the server is intended for loopback use only.
- The in-memory per-run event cache grows with run size; reduction reads
  the whole JSONL log per summary request (fine for build-scale logs, not
  benchmarked for very large runs).
- `domain` and `cmd/testmerge` show 0% Go coverage because they are
  constants/structs and thin wiring exercised indirectly via other
  packages and the live demo.
- No CI configuration file is shipped; the commands above are the
  canonical verification.
