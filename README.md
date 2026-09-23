# Event-Time Session Windows (Java, pure backend)

A dependency-free Java library and JSON service for **event-time session
windowing** over an out-of-order event stream. Time and scheduling are
injectable, watermarks and allowed-lateness jointly decide when windows close,
late events can bridge two windows, and result changes are emitted as explicit
**retraction + addition** records. A small-data exact offline reference
implementation is provided for verification.

No external message system, no frameworks, no third-party dependencies — only
the JDK (`com.sun.net.httpserver` for the HTTP transport). Built with plain
`javac`.

---

## 1. Semantics

### Session formation (per key)

Two consecutive events (ordered by event time) belong to the same session iff
their distance is **≤ `gap`**. A session is described by the span

```
[start, end)   start = min timestamp,   end = max timestamp + gap
```

Equivalently, an arriving event at time `t` — whose own window is
`[t, t+gap]` — merges into a session span `[start, end]` when the intervals
overlap or touch at an endpoint:

```
start − gap ≤ t ≤ end
```

Both endpoints are inclusive, so a gap-sized distance (`t == end`, or
`t+gap == start`) keeps events together; a distance of `gap+1` splits. This
matches the offline reference exactly.

### Watermarks and allowed lateness

- A session is **sealed** when `watermark ≥ end` (`SEALED`). The result is
  final *unless* a sufficiently-late event reopens it.
- A late event is **dropped** when `timestamp < watermark − allowedLateness`
  (`DROPPED`). The boundary is inclusive: `timestamp == watermark −
  allowedLateness` is still accepted.
- A sealed session stays reopenable until
  `watermark − end ≥ allowedLateness`, after which its state is **purged**
  (`PURGED`) and can never be revived.
- A late event within lateness can touch **two** sessions at once (a bridge):
  both old results are `RETRACT`ed and the merged window is `ADD`ed and, if
  already behind the watermark, immediately re-`SEALED`d.
- Watermarks are monotonic; a non-advancing watermark is ignored.

### Result update stream (no in-place mutation)

Every change is an explicit record — the library never silently overwrites a
previously emitted answer:

| record      | meaning                                                            |
|-------------|-------------------------------------------------------------------|
| `ADD`       | a provisional (or new, post-retraction) window aggregate           |
| `RETRACT`   | a previously emitted window (`windowStart`,`windowEnd`) is revoked |
| `SEALED`    | an ADD has become final at the watermark                           |
| `PURGED`    | state for a final session has been deleted                         |
| `DROPPED`   | an event arrived beyond allowed lateness and was ignored           |
| `WATERMARK` | informational marker for watermark advancement                     |

`Results.fold(records)` shows how a consumer materializes the table: `ADD` /
`SEALED` put an entry keyed by `(key, windowStart, windowEnd)`, `RETRACT`
removes the addressed entry (so a window whose end moved is replaced, not
duplicated).

---

## 2. Layout

```
src/com/example/sessionwindow/
  model/      Event, Aggregate, ResultRecord
  engine/     SessionWindowEngine (online), ReferenceGrouper (exact offline),
              Results (record folding), WatermarkGenerator, Session
  time/       Clock, TimerService, ScheduledTimerService (real), ManualTimerService
  json/       Json (parser/serializer), JsonException
  service/    BatchProcessor (stateless + reference check), Pipeline,
              SessionWindowService (stateful registry), ServiceConfig,
              Requests, HttpServerRunner
  Main.java   CLI: `serve` and `run`
test/...      44-test suite with a tiny built-in runner (no JUnit needed)
samples/      bridge.json, boundary.json, late.json
```

Time/scheduling injection: the engine itself is pure event-time (events +
explicit watermarks). Processing-time concerns sit behind interfaces —
`Clock` and `TimerService` — with a real daemon-thread implementation
(`ScheduledTimerService`) and a deterministic virtual clock
(`ManualTimerService`) used by tests.

---

## 3. Build / test / run

Requires JDK 17+ (developed/tested on JDK 21). No Maven/Gradle.

```bash
./build.sh           # javac -> build/classes
./test.sh            # build + run all 44 automated tests
./run.sh samples/bridge.json     # one batch request through the CLI
```

Start the JSON HTTP service:

```bash
java -cp build/classes com.example.sessionwindow.Main serve --port 8080
```

---

## 4. HTTP API

| method | path                                          | purpose                                    |
|--------|-----------------------------------------------|--------------------------------------------|
| GET    | `/health`                                     | liveness                                   |
| POST   | `/session-windows/run`                        | stateless batch; also returns offline comparison + cleanup status |
| GET    | `/session-windows/pipelines`                  | list pipeline ids                          |
| POST   | `/session-windows/pipelines/{id}`             | create with config                         |
| GET    | `/session-windows/pipelines/{id}`             | retained-state snapshot                    |
| POST   | `/session-windows/pipelines/{id}/events`      | ingest items; returns drained records      |
| DELETE | `/session-windows/pipelines/{id}`             | delete pipeline                            |

### Request shape (`/run` and `/events`)

```json
{
  "gap": 10,
  "allowedLateness": 20,
  "flushAtEnd": true,
  "items": [
    {"key": "u1", "timestamp": 0,  "value": 1.0},
    {"key": "u1", "timestamp": 15, "value": 4.0},
    {"watermark": 25},
    {"key": "u1", "timestamp": 10, "value": 9.0},
    {"watermark": 200}
  ]
}
```

`value` defaults to `1.0`. Items are processed in array order; each item is
either an event or `{"watermark": n}`. A bare `events` array (no watermarks)
is also accepted. `/run` returns `records`, the materialized `results`, the
`offlineReference`, `matchesOfflineReference`, `droppedEvents`, and (with
`flushAtEnd`) `stateAfterFlush`.

Create-pipeline config:

```json
{"gap": 10, "allowedLateness": 5, "autoWatermark": false, "outOfOrderness": 0}
```

With `autoWatermark: true`, a punctuated watermark
(`maxEventTimestamp − outOfOrderness`) is advanced whenever a new maximum
event timestamp arrives. With `false`, watermarks are supplied explicitly via
items (or a `TimerService`).

### curl

```bash
curl -s -X POST http://127.0.0.1:8080/session-windows/run \
  -H 'Content-Type: application/json' -d @samples/bridge.json
```

---

## 5. Acceptance scenarios

All three required scenarios are encoded as automated tests *and* sample
requests:

1. **Out-of-order bridge** — `samples/bridge.json`,
   `SessionWindowEngineTest.lateEventBridgesTwoSealedSessionsWithRetractAndAdd`.
   Events `0,15` (gap 10) form two sessions; watermark 25 seals them; a late
   event at `10` (window `[10,20]`) touches both and bridges into `[0,25]`,
   emitting `RETRACT, RETRACT, ADD, SEALED`.
2. **Boundary interval equal to the gap** — `samples/boundary.json`,
   `touchingAtExactlyGapMergesBothSessions` / `eventOneBeyondGapDoesNotBridge`.
   Distance exactly `gap` merges (endpoint touch); `gap+1` splits.
3. **Late event after sealing** — `samples/late.json`,
   `lateEventWithinLatenessReopensSealedSession` and
   `eventBeyondAllowedLatenessIsDroppedAndStateUntouched`. A late event within
   lateness reopens a sealed session; one beyond it is dropped with no state
   change.

**Offline cross-check**: `ReferenceGrouper` sorts each key and cuts sessions
from the complete stream. `ReferenceEquivalenceTest` runs 400 randomized
out-of-order streams (deterministic seeds), interleaves watermarks, flushes,
and asserts the folded online table equals the offline grouping and that all
state is cleaned. `BatchProcessor` performs the same comparison per request.

**State cleanup**: purging is tested directly (`flushPurgesAllState`,
`watermarkSealsSessionsAtEnd`, `eventBeyondAllowedLatenessIsDropped…`), and
every `/run` response reports `retainedKeys` / `retainedSessions` /
`allStateCleaned`.

---

## 6. Observed run results

Commands were actually executed (JDK 21, Linux):

```
$ ./test.sh
...
tests: 44, passed: 44, failed: 0
```

`$ ./run.sh samples/bridge.json` — bridge window materializes as
`u1 [0,25] count=3 sum=14`, with the record sequence
`… SEALED, SEALED, RETRACT, RETRACT, ADD, SEALED … PURGED …`,
`matches offline reference: true`,
`state after flush: retainedKeys=0 retainedSessions=0 allCleaned=true`.

`$ ./run.sh samples/boundary.json` — two sessions `[1,11]` and `[12,17]`
(distance 6 splits; distance 5 merged), reference match, state cleaned.

`$ ./run.sh samples/late.json` — sealed `[0,10]` reopened by late `t=6`
(`RETRACT → ADD[0,16] → re-SEALED`), then a later `t=9` `DROPPED` after
watermark 30; reference match, state cleaned.

Live HTTP verification (server started on port 8080, curl):
`/health` → `{"status":"UP"}`; `/run` returned `matchesOfflineReference: true`
and `allStateCleaned: true`; a stateful pipeline (`gap=10,
allowedLateness=100`) produced, on the late bridge, exactly
`RETRACT[0,10], RETRACT[15,25], ADD[0,25] count=3, SEALED[0,25]`, and its
status snapshot showed one retained sealed merged session before deletion.

No test failures at delivery. During development seven intermediate failures
were found by the suite and fixed (a boundary predicate that needed
recompilation to take effect, two wrong hand-computed test expectations, and a
`ConcurrentModificationException` when purging emptied a key during iteration);
the fixes and the green rerun are part of the history.

---

## 7. Design notes / non-goals

- Pure backend, no UI/frontend.
- In-memory state only; pipelines are a demo registry, not a durable cluster.
- Timestamps are epoch-like `long` milliseconds; aggregates are doubles.
- The built-in JSON parser handles the request/response shapes used here; it is
  not a general-purpose JSON5 parser.
