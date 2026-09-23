# intervalai — interval abstract interpretation backend

A from-scratch, **backend-only** toolchain for a small imperative integer
language called **Imp**. It parses Imp with its own hand-written lexer and
recursive-descent parser, lowers it to an integer-IR control-flow graph, and
runs an **interval abstract interpretation over mathematical integers** with
**widening and narrowing for loops**. It reports where division-by-zero and
array-index out-of-bounds (including **negative** indices) *may* occur — and
it never mistakes an unknown interval `[-∞,+∞]` for safety.

An independent concrete interpreter is used as the execution oracle; the
acceptance tests exhaustively run bounded input spaces against real execution
and demonstrate both precision and the conservativity boundary.

> No part of parsing or analysis delegates to an external compiler or
> compiler framework. Only the Python 3 standard library is used.

## Repository layout

```
intervalai/
  lexer.py          hand-written lexer, source locations
  parser.py         recursive-descent parser -> AST
  ast_nodes.py      AST (every node keeps a source span)
  resolve.py        name/type resolution + initializer lowering
  ir.py             integer IR + CFG (blocks, back-edge/loop-header detection)
  intervals.py      interval lattice over Z; arithmetic; widen/narrow
  abstract_env.py   abstract environments; expression eval; assume/refine
  analyzer.py       worklist fixpoint: widening, narrowing, alarm collection
  concrete.py       independent concrete reference interpreter (oracle)
  pipeline.py       parse/resolve/lower/analyze/execute entry points
  service.py        JSON service (stdio line protocol + HTTP)
  cli.py            command-line convenience wrapper
docs/
  syntax.md         full Imp language definition
  analysis.md       abstract-domain and fixpoint design
examples/           *.imp programs and req_*.json request samples
tests/              unit, acceptance (exhaustive), and randomized tests
run_tests.sh        runs everything and prints CLI demos
```

## Requirements

* Python 3.10+ (developed on 3.12); standard library only, no `pip install`.

## Language in one minute

```
input n;                       # externally supplied integer
arr a[5] = [0,0,0,0,0];        # fixed-size integer array, literal init
var i = 0;
while (i < n) {
  a[i] = i * i;                // in-bounds iff n <= 5
  i = i + 1;
}
```

Arithmetic is over ℤ (`+ - * / %`, floor `/` and `%`, arbitrary precision).
Indices must satisfy `0 <= i < size`; negative indices are out of bounds.
Full grammar and semantics: [`docs/syntax.md`](docs/syntax.md).

## JSON service

### stdio (line protocol)

Each input line is one request, each output line one response:

```bash
cat examples/req_analyze_oob.json | python3 -m intervalai.service
```

### HTTP

```bash
python3 -m intervalai.service --http --port 8000
# POST /analyze | /execute | /parse ; GET /health
curl -s -X POST http://127.0.0.1:8000/analyze \
  -H 'Content-Type: application/json' \
  --data @examples/req_analyze_oob.json
```

### Request shapes

Analyze (interval endpoints are decimal **strings**, for arbitrary-size ints;
omit `input_bounds` to analyze fully unknown inputs):

```json
{"op": "analyze",
 "source": "...Imp source...",
 "input_bounds": {"n": {"lo": "0", "hi": "5"}},
 "include_points": false,
 "include_cfg": false}
```

Execute concretely:

```json
{"op": "execute", "source": "...", "inputs": {"n": 4}, "step_limit": 1000000}
```

Parse (optionally with IR/CFG dump):

```json
{"op": "parse", "source": "...", "include_cfg": true}
```

### Alarm object

```json
{"kind": "index_out_of_bounds",
 "subkind": "index_too_large_possible",
 "severity": "possible",
 "loc": {"line": 5, "col": 2, "start": 40, "end": 41},
 "size": 3,
 "index": {"lo": "0", "hi": "5"},
 "message": "array index may be >= array size 3"}
```

`kind` is `div_by_zero` or `index_out_of_bounds`; `severity` is `definite`
(e.g. divisor exactly `[0,0]`) or `possible`. Errors come back as
`{"status":"error","error":"...","message":"...","loc":{...}}`.

## Command line

```bash
python3 -m intervalai.cli analyze examples/p1_safe_loop.imp --bound n=0..5
python3 -m intervalai.cli analyze examples/p8_unbounded_loop.imp   # unknown input
python3 -m intervalai.cli execute examples/p2_oob_loop.imp -D n=2
python3 -m intervalai.cli parse   examples/p5_negative_index.imp --cfg
```

## How the analysis works (summary)

* Intervals `[l,h]` over ℤ with `±∞` and a bottom element; joins are convex
  hulls, meets are intersections.
* Arithmetic is exact at finite corners (`*`, floor `/`,`%`) with sign-split
  rules for infinite endpoints; division splits the divisor around zero.
* Arrays are **smashed** to one element interval with weak updates.
* Branches refine states (`assume`): constant comparisons intersect
  half-lines; variable–variable comparisons propagate known bounds (e.g.
  `i < n`, `n ≤ 5 ⇒ i ≤ 4`), which keeps loop indices precise.
* Loops use a worklist fixpoint with **standard widening** at loop headers
  (unstable bounds jump to infinity) followed by bounded **narrowing** rounds
  (infinite bounds pulled down to stable finite bounds).
* Any operation whose abstract operand interval intersects an error region
  raises an alarm — an unknown `[-∞,+∞]` therefore always alarms rather than
  being treated as safe.

Details and the conservativity discussion: [`docs/analysis.md`](docs/analysis.md).

## Acceptance: exhaustive inputs vs. real execution

`tests/diff_engine.py` runs every input in the Cartesian product of the
declared ranges through the concrete interpreter, records the faults that
actually occur, and compares them to the analyzer's alarms:

* **soundness** — every real fault is covered by an alarm (no false negatives);
* **exact** hand cases — additionally no false positives;
* **imprecise** hand cases — a specific, documented false positive is required,
  pinning down the conservativity boundary.

Run everything:

```bash
./run_tests.sh                 # or: python3 -m unittest discover -s tests -v
```

Curated cases (`examples/`):

| Program | input range | point demonstrated |
|---|---|---|
| p1_safe_loop | n ∈ 0..5, size 5 | loop index proven in bounds — no alarm |
| p2_oob_loop | n ∈ 0..5, size 3 | real OOB at n=3,4,5, exact alarm |
| p3_diseq_guard | d ∈ -2..2 | `d != 0` leaves an interior hole intervals can't represent → documented false positive |
| p4_positive_guard | d ∈ -2..3 | `d > 0` ⇒ divisor ∈ [1,3], proven safe |
| p5_negative_index | i ∈ -2..2 | negative index OOB detected as such |
| p6_correlation | x ∈ -1..1 | `y=x` correlation lost: divisor always 1 but abstracted to [-1,3] → documented false positive |
| p7_definite_divzero | — | *definite* division by zero |
| p8_unbounded_loop | unknown | `[-∞,+∞]` input is alarmed, never assumed safe |
| p9_arithmetic | a∈2..5,b∈1..4 | exact floor `/`,`%`; one safe index, one real OOB |
| p10_load_store_oob | n ∈ 0..5, size 3 | both the store and the load `a[i]` fault at distinct lines |
| p11_nested_loops | n ∈ 0..5, size 5 | nested loops; selective widening proves `j<i≤4` in bounds |

Plus `tests/test_fuzz.py`: 1,500 seeded randomly-generated programs (37,500
concrete runs) containing loops, nested loops, computed/negative indices,
smashed arrays, and guarded and unguarded divisions — **0 missed faults**
while ~1,000 programs exhibit expected conservative warnings.

## Known conservativity (by design)

* Non-relational: independent variables lose correlations (`p6`).
* No set union / interior holes (`p3`).
* Array smashing: per-cell values are merged.

These can only cause false positives, never missed faults; see
[`docs/analysis.md`](docs/analysis.md#8-conservativity-boundaries-known-imprecision).

## Scope / non-goals

Pure backend: no parser-UI, editor, or graphical frontend. No functions,
floats, strings, or machine-width integers.
