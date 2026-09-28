# Symbolic Provenance for Positive Relational Queries

A service that answers **positive relational queries** (selection, projection,
join, union) and returns, for every answer tuple, its **symbolic provenance
polynomial** over the provenance semiring **N[X]**.

* Repeated derivations of the same answer **add**.
* Each successful join binding **multiplies** its input-row witnesses.
* A row joined with itself keeps the repeated factor: `x * x = x²` (provenance
  is **not** idempotent), so a set of input ids can never be the answer.
* Canonicalization (sorting factors, collecting like monomials) bounds growth
  without changing the polynomial's value.
* NULL follows SQL three-valued logic over an explicitly limited scope.
* Answers and provenance are always read from **one pinned input version**.

Stack: **Python 3.12 · FastAPI · SQLite** (synthetic local fixtures only).

---

## 1. Project layout

```
app/
  config.py                  # env-driven configuration
  main.py                    # uvicorn entrypoint (app.main:app)
  observability/__init__.py  # request correlation, redaction, decision logs
  api/
    models.py                # pydantic request/response models
    app.py                   # FastAPI factory + routes + error envelope
  provenance/
    expressions.py           # N[X] polynomial semiring (the math core)
    language.py              # rule language: relational-algebra AST + parser
    engine.py                # provenance-aware relational evaluation
    store.py                 # versioned SQLite evidence store
    loader.py                # load synthetic SQL fixtures into a version
fixtures/data/*.sql          # local synthetic business data
tests/                       # independent unit, integration and e2e tests
docs/architecture.md         # module responsibilities and data flow
```

The four responsibilities demanded by the brief are real, separate modules:

| Responsibility | Module |
|---|---|
| Rule language | `app/provenance/language.py` |
| Reasoning / evaluation kernel | `app/provenance/engine.py` (+ math in `expressions.py`) |
| Evidence storage | `app/provenance/store.py` (+ `loader.py`) |
| Query interface | `app/api/` |

Tests and configuration live outside the package (`tests/`, `app/config.py`,
env vars).

---

## 2. Setup from a clean directory

```bash
cd b
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Pinned versions (see `requirements.txt`):

| Package | Version |
|---|---|
| fastapi | 0.115.6 |
| uvicorn[standard] | 0.34.0 |
| pydantic | 2.10.4 |
| httpx | 0.28.1 (test client) |
| pytest | 8.3.4 |
| Python / SQLite | 3.12 / 3.45 |

Run the server:

```bash
uvicorn app.main:app --host 127.0.0.1 --port 8077
```

---

## 3. Configuration (environment variables)

| Variable | Default | Meaning |
|---|---|---|
| `PROVENANCE_DB_PATH` | `data/provenance.db` | SQLite evidence-store file |
| `PROVENANCE_FIXTURE_DIR` | `fixtures/data` | fixture directory scanned on load |
| `PROVENANCE_FIXTURE_GLOB` | `*.sql` | fixture file pattern |
| `PROVENANCE_LOG_LEVEL` | `INFO` | log level |
| `PROVENANCE_MAX_QUERY_NODES` | `200` | reject larger parsed plans |

No production accounts or secrets are required.

---

## 4. Fixture SQL format

Plain SQLite DDL/DML with business table/column names. Two optional reserved
columns attach provenance metadata and are stripped from witnessed tuples:

* `__row_id TEXT` — explicit witness id (unique per table; required if you want
  stable, readable names like `e1`)
* `__weight REAL` — numeric weight injected during verification

Tables without `__row_id` get deterministic ids `<table>#<n>`. Native SQL
`NULL` is preserved. See `fixtures/data/01_company.sql`.

Load the fixture directory as a new immutable input version:

```bash
curl -s -X POST http://127.0.0.1:8077/admin/load-directory \
  -H 'Content-Type: application/json' -d '{"label":"company"}'
# {"version_id":1,"reused":false,"loaded":["dept","emp","lead"]}
```

You can also load an inline script via `POST /admin/load-sql`
(`{"sql": "...", "label": "..."}`).

---

## 5. Rule language

A query is JSON relational algebra over operators:

| `op` | shape |
|---|---|
| `relation` | `{"op":"relation","relation":"emp","alias":"l"}` (alias optional, required for self-joins) |
| `select` | `{"op":"select","condition":{...},"input":<node>}` |
| `project` | `{"op":"project","columns":["l.eid"],"input":<node>}` |
| `join` | `{"op":"join","left":<node>,"right":<node>,"on":[["l.dept","d.dept"]]}` |
| `union` | `{"op":"union","left":<node>,"right":<node>}` (set-compatible inputs) |

Selection conditions: `{"col":"l.dept","op":"=","value":"Eng"}` with operators
`= != < <= > >=`, plus the NULL-aware `{"col":"d.budget","op":"is_null"}` /
`"is_not_null"` (no `value`).

Column names may be bare (`dept`) when unambiguous, or qualified
(`l.dept`). Full request bodies are in `examples/`.

### NULL scope

* NULL passes through projection and joins as a value.
* NULL **never** satisfies a comparison (`NULL = 1`, `NULL <> 1` both drop it).
* NULL never joins, not even to another NULL.
* `is_null` / `is_not_null` are the only NULL-aware predicates.
* Writing a comparison against a JSON `null` literal is rejected up front with
  category `NULL_PREDICATE_NOT_SUPPORTED`.

---

## 6. Worked example (request samples)

**Join → project → union** (`examples/query_union.json`):

```bash
curl -s -X POST http://127.0.0.1:8077/query \
  -H 'Content-Type: application/json' --data @examples/query_union.json
```

The answer `("alice",)` has two join derivations (`emp.e1*dept.d1`,
`emp.e6*dept.d1`) plus one union derivation (`lead.l1`):

```
lead.l1 + dept.d1*emp.e1 + dept.d1*emp.e6
```

**Self-join** (`examples/query_self_join.json`) — `emp` joined with itself on
`dept`. The `("alice","alice")` answer is

```
emp.e1^2 + 2*emp.e1*emp.e6 + emp.e6^2
```

(the two ordered cross-bindings canonicalize to coefficient `2`).

**Numeric verification** — inject the fixture weights and compare against an
independently computed expectation (`examples/verify.json`):

```bash
curl -s -X POST http://127.0.0.1:8077/verify \
  -H 'Content-Type: application/json' --data @examples/verify.json
# weights e1=2, e6=9 -> 2·2 + 2·2·9 + 9·9 = 4 + 36 + 81 = 121
```

Each returned row gives `numeric_value` and `matches` (true iff it equals the
optional `expected_value`).

---

## 7. How verification is kept honest

The acceptance tests do **not** generate expected answers from the core under
test (`tests/test_acceptance_independent.py`):

* Input rows are hard-coded in the test module (mirroring the SQL fixture).
* Expected **numeric** results come from an independent, naive weighted
  bag-semantics enumerator written in the test file (shares no code with the
  engine).
* Expected **symbolic** polynomials for the headline answers are hand-derived
  and written literally.
* Every service answer must satisfy both: literal symbolic equality (headline
  rows) and numeric equality against the independent enumerator (every row).
* Failure paths assert the **specific error category**, not "the endpoint
  responded".

---

## 8. Diagnostics

Every request gets a correlation id (inbound `X-Request-Id` honored, otherwise
generated), echoed in the `X-Request-Id` response header and in every JSON log
line and error body. Decision logs state why a query was **accepted**,
**rejected**, or **undecided**, with the version id and answer count. Sensitive
keys (passwords/tokens/secrets/…) are masked and long values truncated.

Error envelope (HTTP 4xx/5xx):

```json
{ "error": "UNKNOWN_COLUMN", "category": "UNKNOWN_COLUMN",
  "detail": "column 'emp.nope' does not exist; available: [...]" ,
  "request_id": "…" }
```

Categories include `MALFORMED_QUERY`, `MALFORMED_REQUEST`, `UNKNOWN_RELATION`,
`UNKNOWN_COLUMN`, `AMBIGUOUS_COLUMN`, `UNSUPPORTED_OPERATOR`,
`NULL_PREDICATE_NOT_SUPPORTED`, `UNION_SCHEMA_MISMATCH`,
`DUPLICATE_OUTPUT_COLUMN`, `UNKNOWN_VERSION`, `NO_INPUT_VERSION`,
`MISSING_WEIGHT`, `QUERY_TOO_COMPLEX`, and fixture/storage categories.

---

## 9. Test commands

```bash
source .venv/bin/activate
python -m pytest                 # whole suite
python -m pytest -m "not e2e"    # exclude HTTP tests if desired
python -m pytest tests/test_acceptance_independent.py -v
```

### Recorded result

On the development machine (Python 3.12.3, SQLite 3.45.1):

```
$ python -m pytest -q
74 passed, 1 warning
```

The 74 tests include the independent acceptance module
(`test_acceptance_independent.py`). A live `uvicorn` + `curl` walkthrough
(sections 4–6) also confirmed:

* `("alice","alice")` self-join polynomial `emp.e1^2 + 2*emp.e1*emp.e6 + emp.e6^2`
* numeric verification `121.0`, `matches: true`
* `NULL` comparison rejected with `NULL_PREDICATE_NOT_SUPPORTED` and the
  supplied `X-Request-Id` echoed back.
