# RESULTS — run record and verification evidence

All commands below were actually executed in this environment. Output is copied
from real runs (timing varies slightly between runs; checks/failures do not).

- Date (UTC): 2026-09-25
- OS: Linux 6.8.0-90-generic x86_64 (Ubuntu 24.04)
- Compiler: g++ (Ubuntu 13.3.0-6ubuntu2~24.04.1) 13.3.0, C++17
- Python: 3.12.3
- No third-party libraries; JSON is parsed by `src/json.cpp`.

## 1. Build

Command: `make`

```
g++ -std=c++17 -O2 -Wall -Wextra -Wpedantic -Wshadow  -Isrc -c src/json.cpp -o build/json.o
g++ -std=c++17 -O2 -Wall -Wextra -Wpedantic -Wshadow  -Isrc -c src/topo.cpp -o build/topo.o
g++ -std=c++17 -O2 -Wall -Wextra -Wpedantic -Wshadow  -Isrc src/main.cpp build/json.o build/topo.o -o topo_server
```

Result: success, **no warnings, no errors**.

## 2. C++ unit + randomized differential tests

Command: `make test`

```
  fuzz seed=12345 accepted=1076 dup=519 cycles=1405 total_visited=56707
  fuzz seed=67890 accepted=2614 dup=585 cycles=2801 total_visited=227790
  fuzz seed=42   accepted=391  dup=614 cycles=995  total_visited=21189

checks=72475 failures=0
```

What is covered: forward inserts, search-triggered reorders, **self-loop**,
**duplicate edges** (idempotent even at the scale cap), **isolated vertices**,
a **200-node reverse-order long chain** plus its rejected backward-closing edge,
cycle-path edge validity (each consecutive pair is a real stored edge),
failed-insertion invariance, and hard scale-cap rejection (low caps
`maxNodes=3, maxEdges=2`). Each fuzz trial compares the incremental decision
with an independent full Kahn recomputation and asserts the maintained order
respects every stored edge.

## 3. Sanitizer build (ASan + UBSan)

Command:

```
g++ -std=c++17 -O1 -g -fsanitize=address,undefined -fno-omit-frame-pointer \
    -Isrc tests/test_topo.cpp src/json.cpp src/topo.cpp -o build/test_topo_san
./build/test_topo_san
```

Result: `checks=72475 failures=0`, **no AddressSanitizer/UBSan reports**
(reported by `run_all.sh` as `sanitizer run: clean`).

## 4. End-to-end JSON differential tests + benchmark

Command: `python3 tests/test_cli.py`

This drives the compiled `./topo_server` binary over its real stdin/stdout JSON
interface and compares every insertion decision with an independent naive Kahn
reference implemented in Python.

```
  differential seed=101: accept=1094 dup=258 cycles=1148 visited_total=62951
  differential seed=202: accept=1888 dup=285 cycles=1827 visited_total=148116
  differential seed=303: accept=501  dup=278 cycles=721  visited_total=19638

== benchmark (bounded scale) ==
scenario                       edges  reorders  visited(total)  visited/edge    time
random-forward n=2000 m=8000    8000      2660            8991          1.12   ~0.06s
random-forward n=20000 m=100000 100000    29669          114128          1.14   ~1.0s
reverse-chain n=5000            4999      4999        12502499       2501.00   ~0.46s

checks=10577 failures=0
```

### How to read the visited-node figures

- **random-forward**: only edges going forward in a (reversed) layout are
  offered; a minority trigger a window repair. The incremental search touches
  roughly **1.1 vertices per edge** even at n=20 000 / m=100 000 — the work
  stays local rather than scanning the graph.
- **reverse-chain n=5000**: every edge `i -> i+1` is inserted against a fully
  reversed layout, so each one forces a repair over a growing window.
  `visited/edge ≈ n/2 = 2501`, cumulative ≈ 12.5 million. This is the honest
  worst case for this bounded-window scheme: cumulative **O(n²)** on an
  adversarial chain. Typical/random inputs stay near-linear per edge.

(Exact wall-clock numbers fluctuate by run; the check counts are stable.)

## 5. Required acceptance cases — explicit confirmation

| Required case | Where verified | Result |
|---|---|---|
| Random insert vs full topo comparison | `tests/test_topo.cpp` fuzz ×3; `tests/test_cli.py` ×3 seeds | pass |
| Self-loop | C++ `testSelfLoop`; Python `test_self_loop` | rejected, `cycle_path:[id]`, no node/edge created |
| Reverse-order long chain | C++ `testReverseLongChain` (n=200); Python (n=400) + bench (n=5000) | order repaired; backward close rejected |
| Isolated vertices | C++ `testIsolatedNodes`; Python `test_isolated_nodes` | present exactly once; order valid |
| Duplicate edges | C++ `testDuplicateEdges`; Python `test_duplicate_edge`; scale-cap test | idempotent, `duplicated:true`, edge count unchanged |
| Cycle rejected + path returned | both suites; path pairs asserted to be real edges | pass |
| Failed insertion does not change graph | C++ invariance checks; Python `test_failed_insert_invariance` | order + node/edge counts unchanged |
| Actual visited-node counting | per-response `visited`; `stats.total_visited`; benchmark | reported above |
| Naive small-scale reference | C++ `IncrementalTopo::verify()` (Kahn); Python `kahn_order` | used as oracle in every trial |

## 6. One-command run

`./run_all.sh` performs: clean build → C++ tests → ASan/UBSan → Python
differential tests + benchmark, ending with `ALL TESTS PASSED`.

## 7. Failures encountered during development (and how they were resolved)

Recorded honestly; none remain in the final code.

1. **Cycle-path endpoint convention (3 failing assertions).** The path was
   initially emitted as `[u, v …, u]`, which included the not-yet-stored
   rejected edge as its first pair. Changed the convention to return the
   **existing** path `[v …, u]` so every consecutive pair is a real stored edge
   and the rejected edge `u -> v` only implicitly closes it. All assertions
   updated; now pass.
2. **Stale binary after an algorithm change.** A smoke test showed the old
   cycle-path format because `make test` only rebuilds the test binary, not
   `topo_server`. Rebuilt the server; `run_all.sh` now does a clean build
   first, so this cannot recur.
3. **Scale-limit precedence.** An initial ordering reported "edge limit" for an
   edge that actually closed a cycle, and an early version interned a vertex
   before rejecting (a mutation on a refused request). Reordered the guards so
   that: duplicates always succeed; self-loops/cycles are detected without
   consuming budget; a new endpoint (unable to form a cycle) is edge-cap
   checked before it is created; and the edge cap for an existing-endpoint
   acyclic edge is enforced immediately before any mutation. A dedicated
   low-cap unit test (`testScaleLimit`) now locks this behavior in.
4. **Hidden O(n) per reorder.** An earlier build rebuilt the external-id order
   array over all vertices after each repair. Removed it; the external order is
   now materialized on demand by `order()`, so the insert path never does a
   full scan.

## 8. Not done / honest limitations

- **No numerical line-coverage %.** `lcov`/`gcov` HTML coverage is not produced;
  `clang-format`, `clang-tidy`, and `cppcheck` are **not installed** in this
  environment (verified: none found on `PATH`). In their place the project
  builds with `-Wall -Wextra -Wpedantic -Wshadow` warning-free and passes a
  clean ASan+UBSan run; the randomized suites exercise every `insertEdge`
  outcome (direct / reorder / duplicate / cycle / cap) thousands of times.
- The bounded-window repair has cumulative **O(n²)** worst-case behaviour on an
  adversarial reversed chain (measured above). It is intentionally simple and
  fully verifiable; a balanced/level-based incremental scheme would improve the
  worst case but was out of scope.
- Interface is stdin/stdout line-delimited JSON only — **no HTTP server and no
  frontend**, as required.
- Vertex ids are signed 32-bit integers; non-integral or out-of-range ids are
  rejected with an error response.
