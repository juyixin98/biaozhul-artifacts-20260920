# Hybrid Logical Clock (HLC) Service

A pure-backend **Hybrid Logical Clock** (Kulkarni, Demirbas, Goyal, Sundaram &
Sridharan, 2014) exposed over HTTP. Written in Go using only the standard
library (`net/http`) — **zero third-party dependencies**.

It supports:

- **local events** (`POST /v1/tick`),
- **receiving remote timestamps** (`POST /v1/receive`),
- **physical clock rollback** (absorbed by keeping the physical part at the max),

and adds two safety mechanisms:

- **future-drift limit** — a remote timestamp absurdly far in the future is
  rejected rather than poisoning this node's clock;
- **bounded logical-counter overflow** — when the logical counter reaches its
  ceiling inside one millisecond, the clock waits (up to a bound) for physical
  time to advance, then resets the counter; if physical time does not advance
  in budget it returns a definite `503 overflow` instead of silently wrapping.

---

## 1. Model

A timestamp is `(physical_ms, logical, node_id)`:

| field | type | meaning |
|-------|------|---------|
| `physical_ms` | int64 | physical component, Unix milliseconds |
| `logical` | uint64 | logical counter, bounded at runtime by `max_logical` (default `2^32-1`) |
| `node_id` | string | issuer id (metadata; never participates in ordering) |

Timestamps are totally ordered lexicographically by `(physical_ms, logical)`.

### Update rules

**Local event**
```
if physical_now > l.physical:            l' = (physical_now, 0)
else:                                    l' = (l.physical, l.logical + 1)
```

**Receive event `m`**
```
l' = max(l.physical, m.physical, physical_now)        # physical part
  · physical_now strictly greatest                  -> counter 0
  · tie on the greatest physical part               -> max(local, remote) counter + 1
  · local physical is the greatest                  -> local counter + 1
  · remote physical is the greatest                 -> remote counter + 1
```

**Future drift.** `m.physical - physical_now > max_drift_ms` ⇒ the message is
rejected with `422 future_drift` and local state is untouched. Default cap
**1000 ms**.

**Physical rollback.** Every rule takes a max over physical values, so moving
the wall clock backwards can never move a timestamp backwards; the node just
keeps issuing logical increments until real time catches up.

**Counter overflow.** A result counter that would exceed `max_logical` does not
wrap. The clock waits for the physical clock to pass the current physical part
and then emits `(new_physical, 0)`; if physical time does not advance within
`--overflow-wait` (default **250 ms**) the call returns `503 overflow`. State
is preserved and the operation may be retried. `--max-logical` is configurable
(useful to exercise overflow in tests).

### Determinism / no real time in test decisions

The physical clock is an injected interface (`hlc.Config.Physical`). All
correctness tests drive a **fake/scripted clock** and assert exact integers —
**no test uses the wall clock to decide pass/fail**. The only real-time use is
an upper-bound assertion that an overflow wait returns promptly.

---

## 2. HTTP API

### `GET /healthz`
Includes `physical_now_ms`, which clients use to build near-future test
timestamps without a separate clock source.
```bash
curl -s localhost:8080/healthz
# {"status":"ok","node_id":"n1","physical_now_ms":1790215099645}
```

### `POST /v1/tick` — local event
```bash
curl -s -X POST localhost:8080/v1/tick
```
```json
{"node_id":"n1","timestamp":{"physical_ms":1790215099740,"logical":0,"node_id":"n1","wire":"hlc://n1/1790215099740:0"}}
```

### `GET /v1/now` — current timestamp without advancing
Same envelope shape as `/v1/tick`.

### `POST /v1/receive` — receive a remote timestamp

Accepts **four** body shapes:
```bash
# 1. wrapped timestamp object
curl -s -X POST localhost:8080/v1/receive -H 'Content-Type: application/json' \
  -d '{"timestamp":{"physical_ms":1790215099000,"logical":7,"node_id":"peer"}}'

# 2. bare timestamp object
curl -s -X POST localhost:8080/v1/receive -H 'Content-Type: application/json' \
  -d '{"physical_ms":1790215099000,"logical":7,"node_id":"peer"}'

# 3. JSON-quoted canonical wire string
curl -s -X POST localhost:8080/v1/receive \
  --data-binary '"hlc://peer/1790215099000:7"'

# 4. raw canonical wire text
curl -s -X POST localhost:8080/v1/receive \
  --data-binary 'hlc://peer/1790215099000:7'
```

The canonical text form is `hlc://<node_id>/<physical_ms>:<logical>`.

**Exact-integer guarantee.** `physical_ms` and `logical` are JSON integers
decoded with `int64`/`uint64` (never `float64`), and the same numbers always
appear in the `wire` string. Numeric fields are also accepted as quoted
decimal strings, so IEEE-754 clients (browser JS) never lose precision.

### `GET /v1/status` — operational snapshot
```json
{
  "last":            {"physical_ms":1790215100949,"logical":8,"node_id":"A","wire":"hlc://A/1790215100949:8"},
  "physical_now_ms": 1790215100185,
  "max_drift_ms":    1000,
  "max_logical":     4294967295,
  "overflow_waits":  0,
  "tick_count":      65,
  "receive_count":   3,
  "drift_rejects":   1,
  "behind_physical": false,
  "skew_ms":         764
}
```
`skew_ms = last.physical - physical_now` (positive means the HLC is ahead).

### Root aliases

`POST /tick` ≙ `/v1/tick`, `GET /timestamp` ≙ `/v1/now`,
`POST /remote` ≙ `/v1/receive`. `GET /` lists endpoints.

### Errors

| HTTP | `code` | when |
|------|--------|------|
| 400 | `bad_request` / `bad_json` / `invalid_timestamp` | empty/malformed body, bad wire form, negative physical, `logical > max_logical`, illegal node id |
| 405 | — | wrong method |
| 422 | `future_drift` | remote physical exceeds the drift cap |
| 503 | `overflow` | logical counter saturated and physical time did not advance in budget |

```json
{"error":"Unprocessable Entity","code":"future_drift","detail":"hlc: remote physical time … is …ms ahead of local physical … (limit 1000ms)"}
```

---

## 3. Build & run

**Requires Go 1.22+** (developed/tested on Go 1.23.4). No CGO, no network at
build time.

```bash
go run ./cmd/hlc-server --addr :8080 --node n1
# or
go build -o bin/hlc-server ./cmd/hlc-server
./bin/hlc-server --addr :8080 --node n1
```

### Flags / environment

| flag | env var | default | meaning |
|------|---------|---------|---------|
| `--addr` | `HLC_ADDR` | `:8080` | listen address |
| `--node` | `HLC_NODE_ID` | hostname | node id on every timestamp |
| `--drift` | `HLC_MAX_DRIFT_MS` | `1000` | future-drift cap, **integer milliseconds** |
| `--max-logical` | `HLC_MAX_LOGICAL` | `4294967295` | logical-counter ceiling |
| `--overflow-wait` | `HLC_OVERFLOW_WAIT_MS` | `250` | max **milliseconds** to wait on a saturated counter |

```bash
./bin/hlc-server --addr=127.0.0.1:8080 --node=node-A \
  --drift=1000 --max-logical=4294967295 --overflow-wait=250
```
Shutdown is graceful on `SIGINT`/`SIGTERM` (5 s drain).

---

## 4. Dependencies (locked)

There are **no third-party modules**, so the lock is the stdlib-only
`go.mod` itself:

```
module hlcservice

go 1.22
```

`go mod tidy` adds nothing and no `go.sum` is produced. Go ≥ 1.22 is the only
build requirement.

---

## 5. Tests

```bash
go test ./...          # all
go test -race ./...    # race detector
go test -cover ./...   # coverage
```

Coverage: `internal/hlc` ~78%, `internal/server` ~79% (race-enabled run).

| test | proves |
|------|--------|
| `TestLocalTickRules` | counter bumps within a ms, resets on physical advance |
| `TestReceiveMergeRules` | every receive branch |
| `TestCausalChainAcrossNodes` | A→B→C→A→B strictly increases under frozen time |
| `TestBurstSameMillisecond` | 100 000 ticks in one ms |
| `TestBurstSameMillisecondConcurrent` | 100 goroutines × 200 ticks: unique, contiguous counters |
| `TestPhysicalClockRollback` | physical moved back 100 ms; no timestamp goes back |
| `TestFutureDriftRejected` | over-budget future rejected; boundary accepted; state intact |
| `TestCounterOverflowWaitsAndRecovers` | saturation waits, then emits `(newPhysical,0)` |
| `TestCounterOverflowTimeout` | bounded wait → `ErrOverflow`, state preserved, still usable |
| `TestConfigurableMaxLogical` | low ceiling triggers overflow; over-limit remote rejected |
| `TestTimestampJSONRoundTrip` | exact int64/uint64 JSON, quoted numbers, wire string, bad shapes |
| `TestParseWire` / `TestValidation` | canonical text form & node/physical validation |
| `internal/server` tests | all four receive bodies, health `physical_now_ms`, routing, 400/404/405/422/503, 64 concurrent ticks |
| `integration_test.go` | two real HTTP nodes; causal chain; large-integer round trip; drift refusal |

---

## 6. End-to-end demo

```bash
scripts/demo.sh             # builds (if needed) and runs all 9 scenarios
PORT_A=9001 ./scripts/demo.sh   # custom ports
```

It brings up three nodes — A/B (drift 1000 ms) and C (huge drift cap,
100 ms overflow wait) — and walks through:

1. health, 2. 64-way concurrent same-ms burst, 3. causal A→B→A chain,
4. +1 h drift rejection (422), 5. exact +1000 ms boundary (200),
6. large exact integers (`2^32-3 → 2^32-2`), 7. JSON-quoted wire body,
8. bounded overflow (503 after ~100 ms), 9. status counters.

Observed causal exchange (fixed physical part, strict increase across nodes):
```
hlc://A/1900000000000:1
hlc://B/1900000000000:2
hlc://A/1900000000000:3
hlc://B/1900000000000:4
hlc://A/1900000000000:5
```
(That pinned-wall form is asserted deterministically in the unit tests; over
real HTTP each curl may cross a millisecond, in which case the physical part
advances and the logical counter resets to 0 — equally correct.)

---

## 7. Layout

```
.
├── go.mod
├── Makefile
├── README.md
├── cmd/hlc-server/main.go        # flags, HTTP server, graceful shutdown
├── internal/
│   ├── hlc/
│   │   ├── timestamp.go          # Timestamp: order, JSON, hlc:// wire, validation
│   │   ├── clock.go              # Tick/Receive, drift cap, bounded overflow, Stats
│   │   └── clock_test.go         # deterministic tests (injected fake clock)
│   └── server/
│       ├── server.go             # mux, healthz, tick/now/status
│       ├── v1.go                 # receive body decoding + error mapping
│       ├── log.go
│       └── server_test.go
├── integration_test.go           # two real HTTP nodes, end-to-end
└── scripts/demo.sh
```

## 8. Notes & limitations

- Single in-process clock; persistence and clustering are out of scope (a
  restart reseeds from the physical clock, as HLC permits).
- `node_id` must be non-empty and contain no whitespace or `:` (the wire
  delimiter). It is echoed but never breaks ordering ties.
- Logical counters are 64-bit on the wire but capped at `max_logical`
  (default 32-bit max); saturation is a safety event, not a silent wrap.
