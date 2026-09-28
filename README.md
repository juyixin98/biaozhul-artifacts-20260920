# Symbolic Provenance for Positive Relational Queries

A small service that answers **positive relational queries** — selection,
projection, join, and union — and returns, for every answer tuple, a **symbolic
provenance polynomial** over the input tuples.

The semantic domain is the provenance semiring **ℕ[X]**:

* an input tuple `t` is a variable `x_t`;
* **duplicate derivations add** — the same answer reached in two different ways
  reads `x + x = 2x` (a set of input ids would wrongly collapse this to `x`);
* **joint dependencies multiply** — a join pairing reads `x*y`, and a self-join
  that pairs a tuple with itself reads `x*x = x**2`;
* evaluating the polynomial at injected numeric weights must equal the weighted
  answer, which the service verifies with an **independent** numeric engine.

## Why it is trustworthy

The acceptance bar is *verifiable* results, not a callable API:

* reference polynomials in `tests/` are derived by hand (repeated rows,
  self-joins, several alternative derivations) and asserted as concrete values;
* a second implementation (`weight_check.py`) propagates plain rationals
  without using `Poly`, and its output is compared to symbolic evaluation —
  a tampering test proves the cross-check actually detects a wrong polynomial;
* tests assert the concrete **failure category** (`rejected_plan`,
  `rejected_input_version`, `indeterminate_type`, `rejected_weights`,
  `rejected_snapshot`), not merely that an endpoint responds.

## Module layout (real responsibilities, not a single script)

```
src/provenance/
  rule_language.py   relational-algebra AST + JSON parser / well-formedness
  polynomial.py      ℕ[X] semantic domain: canonical normal form + evaluation
  engine.py          pure annotated-relational operators (select/project/join/union)
  planner.py         single-version pinning, binding/schema flow, orchestration
  evidence_store.py  SQLite: versioned input evidence, hashes, answer+provenance
  weight_check.py    independent numeric-weight propagation + cross-check
  diagnostics.py     JSON logs with request/record id, key state, redaction
  config.py          local configuration (env overrides)
  api.py             FastAPI query interface
tests/               unit + integration + HTTP tests with hand-derived answers
fixtures/            synthetic local input (graph_v1.json)
examples/            ready-to-post request bodies
config/dev.env       local configuration
scripts/load_fixture.py
```

## Behavioural contracts

1. **Addition vs. multiplication.** Duplicate contributions form sums; joint
   dependencies form products. The answer is never reduced to a set of ids.
2. **Normalisation preserves semantics.** Polynomials are stored in a canonical
   form (sorted monomials, merged like terms, no zero coefficients). This only
   controls growth; equality and evaluation are ring identities.
3. **Bounded NULL support.** The only policy is SQL-style three-valued logic.
   A comparison with NULL is `UNKNOWN`; such rows are excluded from the positive
   answer and logged as *indeterminable*. Cross-type ordering (e.g. `"a" < 1`)
   is rejected as `indeterminate_type` rather than guessed.
4. **One input version per query.** Every `relation` leaf must pin the same
   version; mixing versions is rejected (`rejected_input_version`). Answers and
   provenance are persisted together under one request id and read back from
   the same committed version.

## Requirements

* Python ≥ 3.11 (developed and verified on Python 3.12.3)
* Local-only dependencies, pinned in `requirements.txt`:
  `fastapi==0.141.1`, `uvicorn==0.30.6`, `pydantic==2.9.2`,
  `httpx==0.27.2`, `pytest==8.3.3`, `pytest-cov==5.0.0`.
* SQLite via the Python standard library. No production accounts or network data.

## Reproduce from a clean directory

```bash
# 1. create and activate an isolated environment
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

# 2. (optional) load local configuration
set -a; . config/dev.env; set +a

# 3. load the synthetic evidence into SQLite
PYTHONPATH=src .venv/bin/python scripts/load_fixture.py fixtures/graph_v1.json

# 4. run the tests (with coverage)
.venv/bin/python -m pytest --cov=src/provenance --cov-report=term-missing

# 5. start the service
PYTHONPATH=src PROVENANCE_DB=data/provenance.db \
  .venv/bin/python -m uvicorn provenance.api:app --host 127.0.0.1 --port 8011
```

### Request examples

Self-join two-hop path with injected weights and on-the-fly verification
(expect `edge.e6**2` for the `(d,d)` self loop and `agreed: true`):

```bash
curl -s -X POST http://127.0.0.1:8011/v1/provenance/query \
  -H 'Content-Type: application/json' \
  -d @examples/query_selfjoin_weights.json
```

Other ready requests:

```bash
curl -s -X POST http://127.0.0.1:8011/v1/provenance/query \
  -H 'Content-Type: application/json' -d @examples/query_select.json

curl -s -X POST http://127.0.0.1:8011/v1/provenance/query \
  -H 'Content-Type: application/json' -d @examples/query_union.json

# read the answer + provenance back from the same committed version
curl -s http://127.0.0.1:8011/v1/runs/demo-selfjoin
```

Load a snapshot over HTTP instead of the script:

```bash
curl -s -X POST http://127.0.0.1:8011/v1/snapshots \
  -H 'Content-Type: application/json' \
  -d "{\"snapshot\": $(cat fixtures/graph_v1.json)}"
```

## Rule language

```jsonc
// relation leaf — version is mandatory
{ "op": "relation", "name": "edge", "version": "v1", "alias": "x" }

// selection: conjunction of column–literal (or column–column) comparisons
{ "op": "select", "child": <node>,
  "predicates": [ { "op": "=", "left": "src", "literal": "a" } ] }

// projection (rows that collapse together have provenance summed)
{ "op": "project", "child": <node>, "columns": ["x.src", "y.dst"] }

// theta join: predicates compare two (qualified) columns; alias self-joins
{ "op": "join", "left": <node>, "right": <node>,
  "predicates": [ { "op": "=", "left": "x.dst", "right": "y.src" } ] }

// bag union (additive); DISTINCT is intentionally out of scope
{ "op": "union", "left": <node>, "right": <node> }
```

Comparison operators: `=`, `!=`, `<`, `<=`, `>`, `>=`.

## Worked example (hand-derived)

`edges`: `e1=(a,b)`, `e2=(b,c)`, `e3=(a,b)` (duplicate of e1),
`e4=(c,d)`, `e5=(a,NULL)`, `e6=(d,d)`.

The two-hop self-join `x.dst = y.src`, projected to `(x.src, y.dst)`:

| answer | multiplicity | provenance |
|---|---|---|
| `(a,c)` | 2 | `edge.e1*edge.e2 + edge.e2*edge.e3` |
| `(b,d)` | 1 | `edge.e2*edge.e4` |
| `(c,d)` | 1 | `edge.e4*edge.e6` |
| `(d,d)` | 1 | `edge.e6**2` |

With weights `e1=2,e2=3,e3=5,e4=7,e6=13`:
`(a,c) → 2·3 + 3·5 = 21`, `(d,d) → 13·13 = 169`. The independent numeric
engine returns exactly these values.

## Diagnostics & sensitive data

Each decision emits one JSON line to stderr with `request_id`, `record_id`,
`outcome` (`accepted` / `rejected` / `indeterminable`), a human reason, and the
key state. Values whose key looks secret (`password`, `token`, `api_key`, …)
are rendered `***REDACTED***`; raw secrets are never logged.

## Configuration

| Env var | Default | Meaning |
|---|---|---|
| `PROVENANCE_DB` | `data/provenance.db` | SQLite database path |
| `PROVENANCE_FIXTURES` | `fixtures` | local fixture directory |
| `PROVENANCE_NULL_POLICY` | `sql_three_valued` | only supported NULL policy |
