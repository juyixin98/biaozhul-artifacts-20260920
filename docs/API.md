# HTTP JSON API — request examples

All requests/responses are JSON. The server listens on loopback
(`127.0.0.1:8099` by default) and performs no authentication — it is a
local build/merge service, not a cloud-facing one.

Start the server:

```bash
go build -o bin/testmerge ./cmd/testmerge
./bin/testmerge \
  -addr 127.0.0.1:8099 \
  -cache-dir ./.testmerge-cache \
  -work-dir  ./.testmerge-work \
  -fixtures-root ./testdata/fixtures
```

`-cache-dir` (durable event logs) and `-work-dir` (per-execution fixture
working directories) are deliberately separate directories.

## 1. Health

```bash
curl -s http://127.0.0.1:8099/healthz
# {"status":"ok"}
```

## 2. Create a run

`tests` optionally declares the full set of test ids expected in the run.
Declared-but-never-observed tests are reported as **incomplete**, never as
passed.

```bash
curl -s -X POST http://127.0.0.1:8099/runs \
  -H 'Content-Type: application/json' \
  -d '{"run_id":"run-42","tests":["test-alpha","test-bravo"]}'
```

Omit `run_id` to get a generated `run-<hex>` id. Creating an existing id
returns `409`.

## 3. Append a single event

```bash
curl -s -X POST http://127.0.0.1:8099/runs/run-42/events \
  -H 'Content-Type: application/json' \
  -d '{
    "event_id":"e-0001",
    "seq_no":1,
    "type":"shard_started",
    "shard":"shard-a"
  }'
```

Attempt events keep **test id** and **attempt id** separate:

```bash
curl -s -X POST http://127.0.0.1:8099/runs/run-42/events \
  -H 'Content-Type: application/json' \
  -d '{
    "event_id":"e-0002","seq_no":2,"type":"attempt_started",
    "shard":"shard-a","test_id":"test-alpha","attempt_id":"a-1","attempt_no":1
  }'

curl -s -X POST http://127.0.0.1:8099/runs/run-42/events \
  -H 'Content-Type: application/json' \
  -d '{
    "event_id":"e-0003","seq_no":3,"type":"attempt_finished",
    "shard":"shard-a","test_id":"test-alpha","attempt_id":"a-1","attempt_no":1,
    "status":"failed","message":"assertion 42 != 7"
  }'
```

- `status` on `attempt_finished` must be one of `passed | failed | cancelled`.
- A repeated `event_id` returns `409` and changes nothing (safe to retry).
- Two different events sharing one `seq_no` return `409` (ambiguous order).
- `run_started` is emitted at run creation and cannot be injected.

## 4. Batch replay (safe to repeat)

Re-stream events after an executor restart. Already-seen `event_id`s are
counted as `duplicates`, never as errors:

```bash
curl -s -X POST http://127.0.0.1:8099/runs/run-42/replay \
  -H 'Content-Type: application/json' \
  -d '{"events":[
    {"event_id":"e-0004","seq_no":4,"type":"shard_finished","shard":"shard-a"},
    {"event_id":"e-0005","seq_no":5,"type":"run_finished","status":"failed"}
  ]}'
```

Response excerpt:

```json
{"received":2,"accepted":2,"duplicates":0,"rejected":[],"summary":{ ... }}
```

## 5. Execute a fixture command

Only executables under `-fixtures-root` may run; argv[0] is a path relative
to that root. Commands are **never** run through a shell, and path
traversal is rejected.

```bash
curl -s -X POST http://127.0.0.1:8099/runs/run-42/execute \
  -H 'Content-Type: application/json' \
  -d '{"command":["gen_events.sh","happy"],"timeout":"10s"}'
```

The fixture's stdout is parsed as newline-delimited JSON events and merged
into the run. Response:

```json
{
  "execution_id": "exec-ab12cd34ef56",
  "record": {
    "command": ["/abs/path/to/fixtures/gen_events.sh","happy"],
    "work_dir": "/abs/.testmerge-work/run-42/exec-ab12cd34ef56",
    "exit_code": 0, "timed_out": false,
    "stdout_lines": 11, "events": [ ... ], "parse_errors": []
  },
  "ingested": 11, "duplicates": 0, "rejected": [],
  "summary": { "status": "failed", "counts": {"passed":2,"failed":1,...} }
}
```

- `"execution_id":"exec-1"` makes the call idempotent across HTTP retries;
  re-running the same execution only produces duplicates.
- `auto_finish: true` appends a server-generated `run_finished` after a
  clean exit (seq_no chosen as max+1).
- A crashing fixture returns `200` with non-zero `record.exit_code`; the
  events it managed to print are retained and the run becomes
  **incomplete**, never passed.

Available fixture scenarios:
`happy | retry | crash | shuffled | duplicates | late-write | cancelled | missing | malformed`.

## 6. Read the merged summary

```bash
curl -s http://127.0.0.1:8099/runs/run-42
```

```json
{
  "run_id": "run-42",
  "status": "failed",
  "counts": {"passed": 1, "failed": 1, "cancelled": 0, "incomplete": 0},
  "total": 2,
  "tests": [
    {
      "test_id": "test-alpha",
      "status": "failed",
      "latest_attempt_id": "a-1",
      "latest_status": "failed",
      "reason": "latest attempt failed",
      "attempts": [
        {"attempt_id":"a-1","shard":"shard-a","attempt_no":1,"status":"failed",
         "started":true,"finished":true,"latest":true,"late_writes":0}
      ]
    }
  ],
  "shards": [
    {"shard":"shard-a","started":true,"finished":true,"status":"finished"}
  ],
  "started": true, "finished": true, "cancelled": false,
  "missing_finish": false,
  "events_applied": 6,
  "late_writes_total": 0
}
```

## 7. Other reads

```bash
curl -s http://127.0.0.1:8099/runs                         # list runs
curl -s http://127.0.0.1:8099/runs/run-42/events           # raw event log
curl -s http://127.0.0.1:8099/runs/run-42/executions       # execution audit
```

## Status semantics (four explicit states)

| level  | states |
|--------|--------|
| attempt | `passed`, `failed`, `cancelled`, or no terminal result yet |
| test   | `passed`, `failed`, `cancelled`, `incomplete` — derived from the **latest attempt only** |
| run    | `passed`, `failed`, `cancelled`, `incomplete` |

A run is `passed` **only** when it has a `run_finished` event, zero
failed/incomplete/cancelled tests, and at least one test. Missing results
(started-but-unfinished attempts or declared-but-unseen tests) and
`run_finished` loss (executor crash) always yield `incomplete`.
