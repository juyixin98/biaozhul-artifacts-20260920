# Tree-Decomposition Width Heuristics (pure backend)

A self-contained C++17 backend for **undirected graph elimination orderings**
and **tree decompositions** using the **minimum-fill heuristic**, plus a
naive small-graph **exact reference**, an **independent verifier**, and JSON
I/O. No external graph/treewidth solver is used anywhere in the core
algorithms; the only third-party piece is the Python standard library used by
the test runner.

> **Terminology guarantee.** The number returned by the heuristic is named
> `heuristic_width` and labeled
> `heuristic_upper_bound_on_treewidth_not_claimed_optimal`. It is **never**
> called the optimal treewidth. The exact reference alone reports
> `optimal_width`.

---

## 1. Build

Requirements: a C++17 compiler and GNU make (developed and measured with
g++ 13.3.0 on Linux x86_64).

```bash
make            # builds ./tdw_cli
make test       # builds, then runs the Python unittest suite
make clean
```

`-Wall -Wextra -Wpedantic -Wshadow -Wconversion -Wsign-conversion` are on.
The compiler emits a number of `-Wsign-conversion` notes because vertex
indices are `int` used to index `std::vector`; every such index is range
checked against `n` when input is parsed (see `src/graph.cpp`) or is produced
by `ctz` over validated bitsets. No other warnings or errors occur.

## 2. JSON interface

All modes read/write JSON. Request shape:

```json
{ "num_vertices": 6, "edges": [[0, 1], [1, 2], [2, 3], [3, 4], [4, 5], [5, 0]] }
```

(`n` is accepted as an alias for `num_vertices`. Vertices are `0..n-1`.)

| Mode | Command | Purpose |
|---|---|---|
| heuristic | `./tdw_cli solve < req.json` | minimum-fill ordering + elimination tree decomposition + embedded independent validation |
| exact reference | `./tdw_cli exact < req.json` | everything above, **plus** naive `n!` permutation optimum (`optimal_width`, `optimal_order`, `permutations_examined`, `heuristic_gap`) |
| verify | `./tdw_cli verify < response.json` | validates a serialized response from scratch; exit `0` valid / `1` invalid / `2` malformed |
| exhaustive census | `./tdw_cli exhaustive --n 5 [--fast]` | every labeled simple graph up to n, compared against an exact method |
| scale evidence | `./tdw_cli bench` | capped-scale timing on cycle / clique / disconnected / random cases |

Input errors (out-of-range endpoint, self loop, duplicate edge, n > 64,
malformed JSON) exit with code `2` and a message on stderr.

Response highlights (`solve`/`exact`):

- `elimination_order` — vertex ids in elimination order;
- `heuristic_width` — `max_i |later-neighbors(order[i])|`;
- `fill_edges` — fill edges introduced by elimination;
- `tree_decomposition.bags[]`, `bag_tree_edges[]`, `width` — one bag per
  elimination step; each bag is `{eliminated vertex} ∪ later-neighbors`;
- `statistics.candidates_considered_per_step` — evidence that all remaining
  vertices were scored at each step;
- `validation` — the independent verifier's report, computed against the
  serialized response object (see §4);
- exact mode only: `optimal_width`, `optimal_order`,
  `permutations_examined`, `heuristic_gap`, `heuristic_matches_optimum`;
- `limits` — documents the size caps and `solver:
  self_contained_no_external_solver`.

## 3. Algorithms

- **Minimum-fill heuristic** (`src/elimination.cpp`): repeatedly eliminate a
  vertex whose removal adds the fewest fill edges; ties break by smaller
  current degree, then smaller id (deterministic). Complexity per step is
  `O(n · d²)` over bitsets.
- **Elimination tree decomposition** (`src/tree_decomposition.cpp`): bag for
  step `i` is `{order[i]}` plus its alive (later) neighbors in the chorded
  graph; bag `i` joins the bag of the earliest-eliminated later neighbor.
  Disconnected graphs produce an elimination *forest*; the extra roots are
  linked to the first root (edges with empty intersection) so the output is a
  single tree on `n` bags, as required.
- **Naive exact reference** (`exactOptimalWidth`): enumerates every `n!`
  permutation with `std::next_permutation`, independently replaying each and
  recording the minimum width. Default cap `n <= 8` (40 320 replays), hard
  cap `n <= 10` (3 628 800). This is the acceptance reference.
- **Self-contained fast exact** (`existsOrderWidthLE` / `fastExactWidth`,
  `--fast` only): DFS over first-eliminations with a degeneracy
  (`max_H δ(H)`) lower bound and degree filtering. Used solely to extend the
  *census* to sizes where `n!` enumeration is infeasible; it is not the
  acceptance reference. Agreement with the naive method is asserted by tests
  on all graphs up to n = 5 and per-graph on the n = 7 counterexample.

## 4. Independent verification

`src/validator.cpp` consumes a **serialized response** and, without calling
the decomposition builder, recomputes:

1. order is a permutation of `0..n-1`;
2. **vertex coverage** — every vertex occurs in some bag;
3. **edge coverage** — every original edge *and* every independently
   re-derived fill edge occurs together in some bag;
4. bag graph is a tree (`n-1` distinct edges, connected, acyclic);
5. **running-intersection property** — for each vertex, the bags containing
   it induce a connected subtree (checked by reachability restricted to host
   bags);
6. width consistency — reported width, `max|bag|-1`, and an independent
   bit-level elimination replay all agree; each bag equals the exact
   elimination bag for its step; reported fill edges equal the replay's fill
   set;
7. honest labeling — a non-`exact` response claiming optimality is rejected.

The Python test suite re-implements checks 2–6 **again** from the JSON
(`tests/run_tests.py`), so acceptance does not rely on the C++ validator
alone.

## 5. Examples

| File | Graph | Expected |
|---|---|---|
| `examples/cycle6.json` | 6-cycle | width 2 (tw of any cycle is 2) |
| `examples/clique5.json` | K5 | width 4, zero fill edges |
| `examples/two_disjoint_triangles.json` | 2 K3 components | width 2, multi-root bag tree |
| `examples/tree7.json` | 7-vertex tree | width 1 |
| `examples/edgeless3.json` | 3 isolated vertices | width 0 |
| `examples/chain_of_three_triangles.json` | triangles joined at vertices | width 2 |
| `examples/minfill_suboptimal_n7.json` | triangle {0,1,2}, all nine edges from it to the independent set {4,5,6}, plus vertex 3 adjacent only to {4,5,6} | **heuristic 5 vs optimum 4** |

```bash
./tdw_cli solve < examples/cycle6.json
./tdw_cli exact < examples/minfill_suboptimal_n7.json
./tdw_cli solve < examples/cycle6.json | ./tdw_cli verify
```

## 6. Evidence actually produced (this machine)

Commands and observed results (full JSON is saved under `evidence/`).

### 6.1 Build and tests

```
$ make
g++ -std=c++17 -O2 -Wall -Wextra -Wpedantic -Wshadow -Wconversion \
    -Wsign-conversion ...   # builds clean apart from int-index notes
$ python3 tests/run_tests.py
Ran 22 tests in ~7-8 s
OK
```

The 22 tests cover known treewidths (cycle/clique/forest/disconnected),
input rejection (bad JSON, range, self loop, duplicate edge, oversize),
standalone `verify` accepting valid output and rejecting (a) a bag tampered
to break edge coverage, (b) a rerouted bag tree breaking *only* the running
intersection, and (c) a false optimality claim, plus the censuses below.

### 6.2 Exact control: known graphs vs naive n! optimum

```
$ ./tdw_cli exact < examples/cycle6.json   # 6! = 720 permutations
heuristic_width=2 optimal_width=2 gap=0
$ ./tdw_cli exact < examples/clique5.json
heuristic_width=4 optimal_width=4 gap=0
$ ./tdw_cli exact < examples/two_disjoint_triangles.json
heuristic_width=2 optimal_width=2 gap=0
$ ./tdw_cli exact < examples/tree7.json    # 7! = 5040
heuristic_width=1 optimal_width=1 gap=0
```

### 6.3 Exhaustive census against the optimum

Naive `n!` reference on **every labeled simple graph** up to n = 6
(33 867 graphs; ~15 s):

```
$ ./tdw_cli exhaustive --n 6
 n  graphs        min-fill-exceeds-optimum  worst gap
 1       1                 0                  0
 2       2                 0                  0
 3       8                 0                  0
 4      64                 0                  0
 5    1024                 0                  0
 6   32768                 0                  0
```

On graphs this small minimum-fill is always optimal — which is exactly why a
naive reference must be paired with a larger, methodically enumerated census
rather than cherry-picked examples. Extending the census with the
self-contained DFS exact to n = 7 (2 097 152 labeled graphs; 6.8 s):

```
$ ./tdw_cli exhaustive --n 7 --fast
total_graphs = 2 131 019
graphs where min-fill exceeds optimum = 140
worst gap = 1
```

Smallest witness (n = 7), cross-checked with the **naive 7! enumeration**
(not just the DFS):

```
$ ./tdw_cli exact < examples/minfill_suboptimal_n7.json
heuristic_width : 5
optimal_width   : 4   (naive_permutation_enumeration, 5040 permutations)
optimal_order   : [4, 5, 0, 1, 2, 3, 6]
gap             : 1
validation.valid: true
```

So the heuristic is genuinely non-optimal on some inputs; its width is
consistently reported only as an upper bound.

### 6.4 Capped-scale timing (`bench`, n up to the 64-vertex bitset cap)

Single runs; microsecond resolution from `steady_clock`, including
decomposition construction and independent validation.

| case | n | edges | heuristic width | fill edges | µs |
|---|---:|---:|---:|---:|---:|
| cycle | 60 | 60 | 2 | 57 | 33 |
| clique | 60 | 1770 | 59 | 0 | 384 |
| two disjoint cliques | 60 | 870 | 29 | 0 | 199 |
| random p=0.2 | 60 | 355 | 35 | 627 | 622 |

Every one of the 24 benchmark rows passes independent validation. Widths for
the structured cases are exact (cycle tw = 2, clique tw = n−1, disjoint
union tw = max of components); random-case widths are heuristic upper
bounds and are not claimed optimal.

### 6.5 Negative verification evidence

```
$ ./tdw_cli verify < evidence/resp_cycle6.json ; echo $?
0
# after deleting vertex 1 from bag 0 (edge {0,1} uncovered):
$ (tamper) | ./tdw_cli verify ; echo $?
1      # uncovered_edges >= 1
```

## 7. Limits and honest caveats

- `num_vertices <= 64` (uint64 adjacency bitsets).
- Naive exact endpoint: default `n <= 8`, hard `n <= 10`; rejected above.
- Naive full census (`exhaustive`): `--n <= 6` (2^15 graphs at n = 6).
  `--fast` census: `--n <= 10`.
- JSON nesting is capped at 256 levels; deeper documents are rejected with
  exit code 2 instead of overflowing the recursive-descent parser stack.
- Timing numbers are single-run wall time on one machine; they evidence
  scale, not benchmark-grade statistics.
- The multi-root link edges emitted for disconnected graphs have empty bag
  intersection; this is legal for tree decompositions (the running
  intersection property only constrains paths of bags sharing a vertex) and
  is exercised by `two_disjoint_triangles`.
- There is no frontend, no network service, and no persistent state; the
  "interface" is stdin/stdout JSON, which keeps the core auditable.

## 8. File layout

```
src/json.{hpp,cpp}              hand-written JSON parser/serializer
src/graph.{hpp,cpp}             graph type + request parsing/validation
src/elimination.{hpp,cpp}       min-fill heuristic, replay, n! exact, DFS exact
src/tree_decomposition.{hpp,cpp} elimination tree decomposition
src/validator.{hpp,cpp}         independent coverage / running-intersection checks
src/main.cpp                    CLI: solve | exact | verify | exhaustive | bench
examples/*.json                 request samples
tests/run_tests.py              unittest suite (independent Python re-checks)
evidence/*.json                 responses/census/bench/verifier outputs captured
Makefile
```
