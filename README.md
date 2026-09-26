# Incremental Topological Order (增量拓扑排序后端)

A dependency-free **C++17 backend** that maintains a topological ordering of a
directed graph under **incremental edge insertion**. When an edge would close a
cycle it is **rejected**, the existing cycle path is returned, and the graph is
left completely unchanged. Duplicate edges are accepted idempotently.

The core solver is implemented from scratch — **no graph/solver library and no
external solver is called**. A naive full-recomputation reference (Kahn) is
included and used both inside the engine (`verify`) and in the test harness to
cross-check every randomized decision.

---

## 1. What it does

- Maintains an array `order` that is always a topological ordering
  (`pos[u] < pos[v]` for every stored edge `u -> v`).
- New vertices are created on demand (endpoints of edges, or explicit
  `add_node`); isolated vertices are first-class.
- Inserting `u -> v`:
  - if `u` is already before `v`, the edge is stored directly (**0 nodes
    visited**);
  - otherwise a **bounded bidirectional search** runs over the position window
    between `v` and `u`:
    - forward set `F` = nodes reachable from `v` inside the window,
    - backward set `R` = nodes that can reach `u` inside the window,
    - if `F ∩ R ≠ ∅` there is a path `v ->* u`; the new edge `u -> v` would
      close a cycle, so the request is rejected and a concrete existing path is
      returned;
    - otherwise the window is repaired by moving `R` to the smallest positions
      and `F` to the largest (relative order inside each block preserved).
- Reports the **number of distinct vertices actually visited** per insertion
  (`visited`) and cumulative totals (`stats.total_visited`), giving verifiable
  evidence that work is bounded to the affected window rather than a full scan.

### Why restricting the search to the position window is complete

The current order is topological, so positions are strictly increasing along
every directed path. A path `v ->* u` can therefore never visit a position
smaller than `pos[v]` or larger than `pos[u]`. Hence such a path — if it exists
— lies entirely inside the window, and two bounded searches are sufficient to
decide reachability.

### Cycle-path convention

`cycle_path` is the **already-stored** directed path from the target `v` back
to the source `u`, e.g. rejecting `3 -> 1` returns `[1,2,3]`. Every consecutive
pair in the path is a real edge; the rejected edge `u -> v` is the implicit
closing edge and is never stored. A self-loop returns `[u]`.

---

## 2. Scale limits (hard caps, enforced before any mutation)

| Resource | Limit |
|---|---|
| vertices | `100 000` |
| edges | `1 000 000` |

Requests that would exceed a cap are refused (`"rejected": true`, with an
`error`) and, like cyclic insertions, **do not mutate the graph**. Query with
the `limits` op.

---

## 3. Building

Requires `g++` with C++17 (developed/tested on g++ 13.3, Ubuntu 24.04).

```bash
make            # builds ./topo_server
make test       # builds and runs the C++ unit + fuzz suite
make clean
```

A one-command verification (build → unit tests → ASan/UBSan → Python
differential tests + benchmark):

```bash
./run_all.sh
```

---

## 4. JSON interface (line-delimited, stdin/stdout)

The server reads one JSON request per line and writes one JSON response per
line. State is held for the process lifetime. No HTTP, no frontend.

### Requests

| `op` | fields | meaning |
|---|---|---|
| `ping` | — | health check |
| `limits` | — | report scale caps |
| `add_node` | `id` (int32) | register an isolated vertex |
| `insert_edge` | `u`, `v` (int32) | insert directed edge `u -> v` (endpoints auto-created) |
| `batch` | `edges`: `[[u,v],...]` | apply many edges in one request |
| `order` | — | current topological order |
| `verify` | — | permutation + edge check + independent Kahn recomputation |
| `stats` | — | node/edge counts, reorders, cumulative visited nodes |
| `reset` | — | clear all state |
| `quit` / `exit` | — | terminate the session |

### `insert_edge` response

```json
{"u":3,"v":1,"ok":false,"duplicated":false,"visited":6,
 "cycle":true,"cycle_path":[1,2,3]}
```

- `ok`: edge is present afterwards (includes duplicates)
- `duplicated`: edge already existed; graph unchanged
- `cycle`: edge rejected because it closes a cycle; graph unchanged
- `rejected` + `error`: refused by a scale limit; graph unchanged
- `visited`: distinct vertices touched by the incremental search this call

### Examples

```bash
./topo_server < examples/basic.ndjson
./topo_server < examples/cycle_and_duplicate.ndjson
```

Interactive:

```bash
printf '%s\n' '{"op":"insert_edge","u":1,"v":2}' '{"op":"order"}' | ./topo_server
```

See `examples/` for ready-made request files.

---

## 5. Tests & verification evidence

Two independent layers, both run by `./run_all.sh`:

1. **C++ unit + fuzz tests** (`tests/test_topo.cpp`)
   - forward inserts, search-triggered reorders, self-loops, duplicate edges,
     isolated vertices, a 200-node reverse-order long chain, cycle-path edge
     validity, failed-insertion invariance;
   - three randomized fuzz campaigns (up to 6 000 insertions) that maintain an
     independent model graph, recompute acyclicity with Kahn after every edge,
     and assert the maintained order respects every stored edge.

2. **Python CLI differential tests** (`tests/test_cli.py`) — drives the
   **actual binary** over its JSON interface and compares each decision with an
   independent naive Kahn reference written in Python. Covers the required
   cases: self-loops, the reverse-order long chain, isolated vertices,
   duplicate edges, cycle-path validity (every pair a real stored edge, no
   repeated vertex), and proof that failed insertions change neither order nor
   graph size. It also prints the visited-node benchmark (see `RESULTS.md`).

The C++ suite is additionally compiled and run with
`-fsanitize=address,undefined`.

> Tooling note: `clang-format` / `clang-tidy` / `cppcheck` / `lcov` are not
> installed in this environment (documented honestly in `RESULTS.md`). The
> build instead uses strict `-Wall -Wextra -Wpedantic -Wshadow` plus
> ASan/UBSan. Line coverage % is therefore not measured numerically; the
> randomized suites exercise every code path in `insertEdge` (direct insert,
> reorder accept, duplicate, cycle) thousands of times.

---

## 6. Files

```
src/json.hpp / json.cpp   minimal dependency-free JSON parser/serializer
src/topo.hpp / topo.cpp   incremental engine + naive Kahn verify()
src/main.cpp              line-delimited JSON server
tests/test_topo.cpp       C++ unit + randomized differential tests
tests/test_cli.py         end-to-end JSON tests vs Python Kahn + benchmark
examples/*.ndjson         request samples
run_all.sh                build + full test pipeline
RESULTS.md                recorded commands, output, and honest status
Makefile
```

---

## 7. Complexity

- Direct insert (`pos[u] < pos[v]`): expected `O(1)` (hash adjacency).
- Reordered insert: search/repair work is proportional to the vertices inside
  the affected position window (`visited` reports the exact distinct count),
  not a full graph traversal — though adversarial inputs such as a fully
  reversed chain can force cumulative `O(n^2)` work; measured figures are in
  `RESULTS.md`.
- Memory: `O(V + E)`.
