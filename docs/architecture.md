# Architecture

## Data flow

```
SQL fixtures ──loader──▶ versioned SQLite evidence store (store.py)
                                  │  schema + rows + weights, under one version_id
JSON query ──language.py (parse/validate, resolve labels)──▶ algebra AST
                                  │
                          engine.py (N[X] K-relation evaluation)
                                  │  tuple -> Polynomial
                                  ▼
                     service.py (pins version, runs, verifies numerically)
                                  │
                          api/ (FastAPI HTTP + diagnostics)
```

The four mandated responsibilities are isolated behind small interfaces so the
math core has no dependency on the web or storage layers:

```
expressions.py  ◀── engine.py ◀── service.py ──▶ api/
                      ▲              │
                language.py          ▼
                                   store.py ◀── loader.py
```

## Provenance semiring: N[X] (`expressions.py`)

A `Polynomial` is an immutable map `monomial -> positive int coefficient`,
where a monomial is a **sorted tuple of witness ids**. Operations:

* `+` union of contributions; equal monomials sum coefficients.
* `*` Cartesian product of term pairs; monomials concatenate and re-sort;
  coefficients multiply.
* Witness multiplication is non-idempotent: `("x",) * ("x",) == ("x","x")`
  (rendered `x²`), which is essential for self-join provenance.
* `evaluate(weights)` substitutes a number per witness and returns the value;
  an unknown witness raises `MissingWeightError` rather than silently becoming 0.
* `render()` is canonical (lowest-degree first, then lexicographic) so output
  is stable for tests and diffs.

Canonicalization is semantics-preserving because N[X] addition and
multiplication are commutative and associative; it bounds expression size but
cannot change the value.

## Rule language (`language.py`)

Parses JSON into a frozen AST and performs **all** name resolution against the
version's schema before execution: relation/alias → qualified labels, bare
columns resolved or flagged ambiguous, join/union compatibility checked. Each
validation failure carries a stable `category`. The engine therefore performs
no name lookups.

## Evaluation kernel (`engine.py`)

Every intermediate relation is a **K-relation**: `dict[tuple, Polynomial]`.

| Operator | Provenance action |
|---|---|
| relation scan | one monomial per witnessed input row; value-equal rows sum |
| select | keep polynomial unchanged for qualifying tuples |
| join | for each matching pair, multiply polynomials, sum by merged tuple |
| project | drop columns, then sum polynomials grouped by the new tuple |
| union | sum polynomials grouped by equal tuples across both bags |

These are precisely the semiring homomorphisms, so the implementation is a
direct transcription of positive relational algebra semantics. Tuple emission
order follows input row ordinals, making results deterministic.

NULL handling: comparisons and join keys treat NULL as failing (SQL
three-valued logic); only `is_null`/`is_not_null` can match NULL.

## Evidence store (`store.py` + `loader.py`)

* `versions`, `relations`, `rows` tables. Every business row is tagged with a
  `version_id`; a version is write-once. A query resolves a single version id
  and reads schema, rows, and weights under it, so answer and provenance can
  never mix versions.
* Relation/column names are stored as **data** (EAV with JSON payloads), never
  interpolated into the issued SQL. The loader reflects a throwaway SQLite
  database built from fixture DDL, strips reserved `__row_id` / `__weight`
  columns, preserves native NULL, and rejects duplicate relation/row ids.

## Service boundary (`service.py`)

Resolves the version (explicit id or latest), loads schema + relations, parses,
evaluates, and serializes. `/verify` rebuilds the polynomial from the
client-provided serialized terms and evaluates them against the pinned
version's weights, optionally comparing to an independently supplied
`expected_value`.

## Interface and diagnostics (`api/`, `observability/`)

FastAPI factory `create_app(settings)`; blocking SQLite endpoints are ordinary
`def` handlers so Starlette runs them in a worker threadpool. A middleware binds
a correlation id (honoring inbound `X-Request-Id`) into a contextvar used by
structured JSON logs and the error envelope. Sensitive keys are masked and
oversized values truncated in logs.

## Determinism and trust

* Symbolic answers are canonical and deterministic.
* Independent acceptance tests hard-code both the input and a separate naive
  weighted interpreter, and compare against the service for **every** answer
  row — expected results are never produced by the implementation under test.
