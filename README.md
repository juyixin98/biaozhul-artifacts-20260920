# Approximate Frequent-Item Detection (Java, backend only)

A small, dependency-free Java library and JSON/HTTP service for **approximate
frequent-item detection over event streams**.

It combines:

- a **Count-Min Sketch** for bounded-over-estimate point frequency queries,
- a **bounded candidate set** for a top-K *projection* with strictly limited
  memory,
- an **exact reference counter** for small-data ground truth / validation,
- **injectable time and scheduling** (a real system clock+scheduler in
  production; a deterministic virtual clock in tests and demos),
- tumbling event-time windows,
- sketch snapshots and **compatibility-checked merging** over JSON.

No external message system, no frameworks, no frontend. The only build
requirement is a JDK (`javac`/`java`); the HTTP server uses the JDK's built-in
`com.sun.net.httpserver.HttpServer` and JSON is parsed/serialized by a tiny
parser in this repo.

---

## 1. What is guaranteed — and what is deliberately not

This distinction is the core of the project, so it is stated first.

### Guaranteed: point counts have a declared one-sided error bound

For a Count-Min Sketch with `width = ceil(e/epsilon)` rows and
`depth = ceil(ln(1/delta))` independent hash functions, over a stream of `N`
updates, for **every fixed item x**:

```
estimate(x) >= trueCount(x)                                    (never under-counts)
estimate(x) - trueCount(x) <= ceil(epsilon * N)                with probability >= 1 - delta
```

The bound is **declared at query time**, in every point-query response:

```json
{ "item": "apple", "estimate": 50, "errorUpperBound": 5,
  "epsilon": 0.0494, "delta": 0.00674, "totalCount": 87 }
```

### NOT guaranteed: the candidate set covers the true top-K

The top-K endpoint returns a **projection of a bounded candidate set**. A real
top-K item can be absent because:

1. the candidate set is capped at `candidateCapacity` items (`recall <=
   capacity/K` by construction), and
2. admission/eviction uses sketch estimates, which collisions can inflate, so
   a collision-amplified low-frequency item can evict a genuine heavy hitter,
   and
3. arrival order and churn matter even when counts are exact.

Every top-K response says so explicitly:

```json
"coverageGuarantee": "none: items are a projection of the bounded candidate
set; a true top-K item may have been evicted. Point counts carry the bound above."
```

To get ground truth, enable `trackExact` (intended for small data, tests, and
the acceptance experiments) and read `exactTopK` in closed-window results.

### Guaranteed: incompatible sketches cannot be merged

Merging two sketches only makes sense when they place every item in identical
buckets. A merge is **rejected** (`SketchIncompatibleException`, HTTP `409`)
unless `width`, `depth`, and `seed` all match. There is no best-effort merge.

---

## 2. Layout

```
src/approx/
  time/      Clock, TaskScheduler, SystemRuntime, ManualEnvironment
  cms/       CountMinSketch, HashFamily, BoundedCandidateSet,
             ExactCounter, SketchSnapshot, SketchIncompatibleException
  stream/    Event, EngineConfig, FrequentItemsEngine, WindowResult
  json/      minimal JSON parser/writer
  server/    HttpEventServer (JDK HttpServer)
  Main.java  server entry point

test/test/
  cms/       CountMinSketch, BoundedCandidateSet, ExactCounter tests
  stream/    engine, window rotation, late events, merge tests
  json/      parser/writer tests
  server/    end-to-end HTTP tests (real server, in-JVM HTTP client)
  exp/       AcceptanceExperiments (skew, collisions, merge, coverage)

scripts/   build.sh, run-tests.sh, acceptance.sh, demo.sh
examples/  sample request bodies
docs/      captured acceptance + demo output
```

---

## 3. Build and run

Requires a JDK (developed and verified on OpenJDK 21; only long-stable APIs are
used).

```bash
# Compile
bash scripts/build.sh

# Start the server (real system clock, port 8080 by default)
java -cp build/classes approx.Main --port 8080

# Or start with a virtual clock you move yourself (deterministic demos/tests)
java -cp build/classes approx.Main --port 0 --manual-time   # port 0 = ephemeral; chosen port is logged
```

### Automated tests

```bash
bash scripts/run-tests.sh
```

Compiles everything from scratch and runs 49 tests (sketch math, candidate
set, exact counter, engine/window semantics, JSON, and end-to-end HTTP). A
non-zero exit code is returned on any failure.

### Acceptance experiments

```bash
bash scripts/acceptance.sh    # writes docs/acceptance-output.txt
```

Runs the four experiments described in section 6 and prints/tabulates results.

### Live end-to-end demo

```bash
bash scripts/demo.sh           # writes docs/demo-output.txt and docs/server.log
```

Starts a manual-time server, creates an engine, sends a skewed batch, queries
counts and top-K, rotates a window via the virtual clock, and performs both an
accepted and two rejected merges.

---

## 4. HTTP API

Base path `/v1`. All bodies are JSON.

| Method & path | Meaning |
|---|---|
| `POST /v1/engines` | create an engine (see below) |
| `GET  /v1/engines` | list engines + stats |
| `GET  /v1/engines/{id}` | one engine's stats |
| `DELETE /v1/engines/{id}` | remove an engine |
| `POST /v1/engines/{id}/events` | append events |
| `GET  /v1/engines/{id}/topk?k=n` | candidate top-K projection |
| `GET  /v1/engines/{id}/items/{item}` | point estimate + error bound |
| `GET  /v1/engines/{id}/sketch` | serializable sketch snapshot |
| `POST /v1/engines/{id}/merge` | merge a compatible snapshot |
| `POST /v1/engines/{id}/flush` | force-close the active window |
| `GET  /v1/engines/{id}/windows` | closed-window summaries |
| `GET  /v1/engines/{id}/windows/{n}?sketch=true` | one closed window |
| `POST /v1/admin/advance-time` | move the virtual clock (manual mode only) |
| `GET  /healthz` | liveness |

### Create-engine body

Either size the sketch explicitly:

```json
{ "id": "demo", "width": 256, "depth": 5, "seed": 42,
  "candidateCapacity": 10, "windowMillis": 0, "trackExact": true }
```

or from an error/confidence target (`width=ceil(e/epsilon)`,
`depth=ceil(ln(1/delta))`):

```json
{ "id": "sized", "epsilon": 0.01, "delta": 0.01,
  "candidateCapacity": 10, "windowMillis": 10000, "trackExact": false }
```

- `windowMillis = 0` means one never-rotating window.
- `trackExact = true` also keeps an exact hash map (small data / validation).

### Event body

```json
{ "timestampMillis": 100,
  "events": [
    "plain-string-item",
    { "item": "apple", "count": 50 },
    { "item": "kiwi", "count": 5, "timestampMillis": 1500 }
  ] }
```

- `count` repeats one event that many times (default 1).
- a missing event timestamp uses the request clock time.
- an event older than the active window start is counted as `droppedLate` and
  never mutates a closed window.

### Errors

| HTTP status | `error` code | Cause |
|---|---|---|
| 400 | `invalid_json` | body is not parseable JSON |
| 400 | `invalid_request` | malformed/typed request fields |
| 404 | `engine_not_found` / `window_not_found` / `not_found` | unknown resource |
| 405 | `method_not_allowed` | wrong verb for a route |
| 409 | `sketch_incompatible` | merge sketch differs in width/depth/seed |
| 403 | `manual_time_disabled` | virtual-clock control on a real-time server |

Sample bodies live in [`examples/`](examples/).

---

## 5. Library usage (no HTTP required)

```java
Clock clock; TaskScheduler scheduler;
SystemRuntime runtime = new SystemRuntime("worker"); // production
// ManualEnvironment env = new ManualEnvironment(0L); // tests: clock + scheduler

EngineConfig config = EngineConfig
        .withErrorTarget(0.01, 0.01, /*seed*/ 42L)
        .candidateCapacity(10)
        .windowMillis(10_000)
        .trackExact(true)
        .build();

try (FrequentItemsEngine engine =
        new FrequentItemsEngine("orders", config, runtime, runtime)) {
    engine.start();
    engine.addEvent(new Event<>("item-7", runtime.nowMillis()));

    long estimate = engine.queryItem("item-7") ...;
    List<Map.Entry<String, Long>> top = engine.currentTopK(10);
    engine.mergeSketch(otherSnapshot); // throws SketchIncompatibleException
}
```

Injecting both `Clock` and `TaskScheduler` is the seam that makes every
time-based behavior deterministic: in tests, `ManualEnvironment.advance(n)`
moves time and fires every scheduled rotation that came due, with no real
sleeping and no background threads.

---

## 6. Acceptance experiments

Implemented in `test/test/exp/AcceptanceExperiments.java`; captured output in
[`docs/acceptance-output.txt`](docs/acceptance-output.txt).

1. **Skew.** A fixed Zipf (s=1.0) sequence of 120,000 events over 1,000
   distinct items is fed to a 272x5 sketch for 20 different seeds and compared
   against the exact counter. Observed (captured run): **0 under-estimates out
   of 20,000 point queries**, aggregate max over-estimate 752 against the
   declared bound `ceil(epsilon*N) = 1200`, mean error ≈ 60.9, zero items past
   the per-item bound.
2. **Crafted collisions.** Strings that share a bucket on *every* hash row are
   found by birthday search. A victim with true count 10 sharing its buckets
   with 120 other items is reported as 130; the inflated estimates then evict
   a genuinely heavy retained item from a capacity-2 candidate set —
   demonstrating why candidate membership carries no correctness guarantee.
3. **Merge compatibility.** Equal width/depth/seed merge pointwise (totals add).
   Different seed and different width are each rejected with a precise error.
4. **Candidate coverage.** Ten equal-weight heavy items plus churn are run
   against capacities 10/7/5/3; measured recall is exactly 1.00/0.70/0.50/0.30,
   i.e. capped at `capacity/K` even with essentially exact counts.

---

## 7. Design notes

- **Hashing.** Each sketch row uses its own seed-derived offset mixed into an
  FNV-1a pass over the UTF-8 bytes, finalized with Murmur3's `fmix64`. The
  mapping is a pure function of `(seed, depth, item, width)`, which is what
  makes same-seed sketches provably mergeable.
- **Sketch merge** is pairwise cell addition on equal layouts; `totalCount` is
  added as well. Snapshots are plain JSON (rows of integer counters), so a
  sketch can be exported, shipped, and merged without sharing JVMs.
- **Windows** are tumbling, aligned to engine start, closed either by a
  scheduler tick or by an out-of-window event arriving, with late events
  dropped and counted. The last 64 closed windows are retained for inspection.
- **Thread safety.** Sketches, candidate sets, and the engine synchronize on
  themselves; the HTTP layer keeps engines in a `ConcurrentHashMap` and uses a
  small daemon thread pool.

## 8. Known limitations / non-goals

- The exact counter keeps every distinct item in a hash map; it is meant for
  small data and validation, not unbounded production streams.
- Closed-window retention is bounded at 64 results per engine.
- There is no persistence, authentication, or frontend (explicitly out of
  scope).
