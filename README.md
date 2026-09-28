# Sturm Real-Root Isolation Service

An **exact** service that isolates the real roots of a rational-coefficient
polynomial into **pairwise disjoint rational intervals**, with a per-interval
proof and three independent witnesses. The core algorithm is the
**Sturm sequence**, computed entirely with Python's exact
[`fractions.Fraction`](https://docs.python.org/3/library/fractions.html)
arithmetic — no floating point is ever trusted to decide a root.

It is built with **Python + FastAPI + NumPy + SciPy + mpmath**, split into
four layers with real, separate responsibilities.

---

## 1. What it does

Given a polynomial `p(x) = a_n x^n + … + a_1 x + a_0` with **exact rational**
coefficients, the service returns:

* the number of **distinct** real roots;
* the number of real roots **counted with multiplicity**;
* the number of complex roots (by degree accounting);
* a set of **pairwise disjoint rational intervals `(a, b]`**, each containing
  **exactly one distinct** real root, together with that root's **multiplicity**
  and a machine-checkable **Sturm proof**;
* independent corroboration from an exact **Descartes/Vincent** witness, a
  high-precision **mpmath** witness, and a **NumPy/SciPy float64** witness;
* a verdict: `accepted`, `indeterminate`, or `rejected`.

### Special cases are first-class, not shoehorned into a root list

* **Zero polynomial** → `result_kind = "zero_polynomial"`. The zero polynomial
  vanishes at *every* real number and has no finite root list, so it is never
  returned through the ordinary interval result.
* **Non-zero constant** → `result_kind = "constant_nonzero"`, zero roots.

---

## 2. The two headline correctness constraints

### 2.1 Square-free factorization distinguishes repeated and different roots

A naive Sturm chain counts **distinct** roots and cannot say anything about
multiplicity. The kernel first computes an exact decomposition

```
p(x) = ∏_k F_k(x)^k
```

where each `F_k` is monic and **square-free**, holding precisely the factors
that occur with multiplicity `k`. It does this using only exact gcd/division:

```
R = gcd(p, p')          # each distinct factor appears with multiplicity m-1
W = p / R               # each distinct factor appears once
```

Sturm isolation runs on the square-free **radical** `W` (one copy of every
distinct root), guaranteeing disjoint intervals. Each interval is then tagged
with its multiplicity by counting roots of every `F_k` inside it — exactly one
factor owns the unique root.

So `(x-1)^2 (x+2)` correctly reports two distinct roots with multiplicities
`{2, 1}`, total 3.

### 2.2 Roots exactly at interval endpoints

Every interval is the **half-open `(a, b]`**. At a point `x` where the
polynomial vanishes, the kernel **deletes the leading zero** from the Sturm
sign sequence. This evaluates the right-hand limit `V(x+)` of the
variation-count step function, so

```
# { roots in (a, b] } = V(a+) − V(b+)
```

Consequences, all exact with no special-case branching during bisection:

* a root exactly at the **left** endpoint `a` is **excluded**;
* a root exactly at the **right** endpoint `b` is **included**;
* when a bisection midpoint happens to equal a root, the half-open partition
  `(a, b] = (a, m] ∪ (m, b]` attributes that root to the left child exactly once.

The response flags `left_endpoint_is_root` / `right_endpoint_is_root` per
interval so endpoint roots are explicit rather than implicit.

---

## 3. Project layout (four real layers)

```
.
├── config/
│   └── settings.py            # typed, env-overridable budgets & numeric config
├── root_isolator/
│   ├── errors.py              # stable error categories + HTTP mapping
│   ├── input/
│   │   └── parser.py          # LAYER 1: exact coefficient parsing, budgets,
│   │                          #          redaction-safe fingerprint
│   ├── kernel/
│   │   ├── polynomial.py      # LAYER 2: exact RationalPoly (Fraction)
│   │   ├── euclidean.py       #          exact divmod/gcd + Sturm chain/signs
│   │   ├── squarefree.py      #          square-free factorization + Cauchy bound
│   │   └── isolate.py         #          half-open bisection isolation + proofs
│   ├── evidence/
│   │   ├── descartes.py       # LAYER 3: exact, INDEPENDENT Descartes/Vincent
│   │   ├── numeric_check.py   #          mpmath arbitrary-precision witness
│   │   ├── float64_check.py   #          NumPy/SciPy float64 witness
│   │   ├── allocator.py       #          shared half-open (a,b] root allocator
│   │   └── verifier.py        #          tri-state verdict + structural checks
│   └── service/
│       ├── diagnostics.py     # LAYER 4: structured, redaction-aware logging
│       ├── application.py     #          framework-free pipeline orchestration
│       ├── schemas.py         #          pydantic request shape
│       ├── serialization.py   #          exact rationals + evidence -> JSON
│       ├── app.py             #          FastAPI adapter (request ids, errors)
│       └── __main__.py        #          uvicorn entrypoint
├── tests/
│   ├── oracle.py              # INDEPENDENT from-scratch sparse Sturm oracle
│   ├── fixtures/root_cases.json  # hand sign table + known-constant answers
│   └── test_*.py              # 165 assertions across units/property/HTTP
├── examples/call_api.py       # live-HTTP or in-process example client
├── requirements.txt           # fully pinned dependencies
└── pyproject.toml
```

This is deliberately **not** a single-file script and **not** an interface-only
skeleton: every layer has independent logic and its own tests.

---

## 4. Exact inputs and explicit budgets

### Coefficients must be exact

Accepted coefficient forms:

* JSON integers: `2`, `-7`;
* decimal strings: `"0.25"`, `"-1.5"`;
* fraction strings: `"3/7"`, `"1/10"`;
* exponent strings: `"1e-3"`.

A binary float such as `0.1` or `1.5` is **rejected** with category
`NON_EXACT_COEFFICIENT` (HTTP 422), because it cannot represent a rational
coefficient exactly. Send `"1/10"` instead.

Two shapes are supported:

* dense, default descending `[a_n, …, a_0]` (`"order": "descending"`), or
  ascending with `"order": "ascending"`;
* sparse: `{"sparse": {"2": 1, "0": "-1/4"}}`.

### Budgets are explicit and bounded (see `config/settings.py`)

| Setting (`RI_*` env var)          | Default     | Guards against                          |
|-----------------------------------|-------------|-----------------------------------------|
| `RI_MAX_DEGREE`                   | 64          | huge-degree exact work                  |
| `RI_MAX_COEFFICIENT_BITS`         | 4096        | enormous coefficients                   |
| `RI_MAX_STURM_PAIRS`              | 2 000 000   | unbounded chain evaluations             |
| `RI_MAX_BISECTION_DEPTH`          | 200         | roots closer than the limit can separate|
| `RI_MAX_ROOTS`                    | 256         | output explosion                        |
| `RI_MPMATH_PREC`                  | 113         | high-precision witness working precision|

Budget exhaustion returns a specific category (`DEGREE_EXCEEDED`,
`COEFFICIENT_TOO_LARGE`, `BUDGET_EXCEEDED`, `TOO_MANY_ROOTS`) rather than
hanging. Reaching the bisection-depth limit returns `INCONCLUSIVE` (HTTP 409):
the answer cannot be certified within budget, which is different from a wrong
answer.

---

## 5. Evidence and the three verdicts

| Verdict          | Meaning                                                                 | HTTP |
|------------------|-------------------------------------------------------------------------|------|
| `accepted`       | exact Sturm **and** exact Descartes/Vincent agree, structural invariants hold, and **both** numeric witnesses confirm | 200  |
| `indeterminate`  | exact answer is produced and certified by exact witnesses, but a numeric witness could not confirm at its precision | 200  |
| `rejected`       | an **exact** witness disagrees (Descartes, disjointness, degree accounting) | 409 (`EVIDENCE_MISMATCH`) |

Numeric witnesses are deliberately **non-authoritative**. Float64 cannot
separate roots `2^-50` apart or resolve a `10^-20` root; in those cases the
exact answer is returned with `indeterminate` and a precise reason, never
silently accepted and never overruled.

The independent witnesses use **different mathematics and no shared
algorithmic code** with the kernel:

* **Exact Descartes/Vincent** — a separate exact root *count* using Descartes'
  rule of signs after an exact Möbius substitution, bisecting until the sign
  variation count is 0 or 1. (A single Descartes transform is only an upper
  bound when variations ≥ 2, so bisection is required for exactness.) It is
  cross-validated against the test oracle on 960+ random half-open intervals.
* **mpmath** — arbitrary-precision `polyroots`, roots clustered at working
  precision and allocated to intervals under the same `(a, b]` rule.
* **NumPy/SciPy** — `numpy.roots` companion eigenvalues plus a SciPy Newton
  polish, clustered to merge split repeated eigenvalues.

Structural invariants checked per response: pairwise interval disjointness,
each Sturm proof has `V(a+) − V(b+) = 1`, and degree accounting
`real(with multiplicity) + complex = degree`.

---

## 6. Running it

### Install (dependencies are pinned in `requirements.txt`)

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
```

### Start the HTTP service

```bash
python -m root_isolator.service
# configurable: RI_HOST=127.0.0.1 RI_PORT=8000 RI_LOG_LEVEL=INFO
```

### Call it

```bash
curl -s -X POST http://127.0.0.1:8000/api/v1/isolate \
  -H 'Content-Type: application/json' \
  -H 'X-Request-ID: demo-1' \
  -d '{"coefficients": [1, 0, -3, 2]}'
```

`(x-1)^2 (x+2)` returns two disjoint intervals with multiplicities `{2, 1}`:

```json
{
  "request_id": "demo-1",
  "verdict": "accepted",
  "result_kind": "isolated",
  "root_counts": {
    "distinct_real_roots": 2,
    "real_roots_with_multiplicity": 3,
    "complex_roots_with_multiplicity": 0
  },
  "intervals": [
    {
      "index": 0,
      "left":  {"numerator": -3, "denominator": 1, "decimal_display": "-3"},
      "right": {"numerator": 0,  "denominator": 1, "decimal_display": "0"},
      "multiplicity": 1,
      "proof": {"theorem": "Sturm",
                "convention": "half-open (a, b]; count = V(a+) - V(b+)",
                "variations_left": 2, "variations_right": 1,
                "distinct_roots": 1, "depth": 1,
                "left_endpoint_is_root": false,
                "right_endpoint_is_root": false,
                "signs_left": [1, -1, 1], "signs_right": [-1, 0, 1],
                "multiplicity_evidence": {"factor_counts": {"1": {"roots_in_interval": 1}, "2": {"roots_in_interval": 0}}}},
    }
  ]
}
```

(Output abridged; the live response also includes every independent witness.)

Rationals are emitted as `{"numerator", "denominator", "decimal_display"}`;
the numerator/denominator pair is authoritative and `decimal_display` is a
clearly-marked, bounded display approximation.

### Endpoints

| Method | Path                | Purpose                                   |
|--------|---------------------|-------------------------------------------|
| GET    | `/health`           | liveness + version                        |
| POST   | `/api/v1/isolate`   | isolate roots; accepts `X-Request-ID`     |

OpenAPI docs are served by FastAPI at `/docs` and `/openapi.json`.

### Examples and tests

```bash
python examples/call_api.py            # live HTTP if up, else in-process
python -m pytest -q                    # full suite
python -m pytest --cov=root_isolator   # coverage (>= 90% overall)
```

---

## 7. Error categories

| Category                    | HTTP | Meaning                                    |
|-----------------------------|------|--------------------------------------------|
| `MALFORMED_COEFFICIENTS`    | 422  | bad shape/type/order, invalid JSON/string  |
| `NON_EXACT_COEFFICIENT`     | 422  | binary float supplied                      |
| `EMPTY_COEFFICIENTS`        | 422  | no coefficients                            |
| `DEGREE_EXCEEDED`           | 422  | degree over budget                         |
| `COEFFICIENT_TOO_LARGE`     | 422  | coefficient bit size over budget           |
| `TOO_MANY_ROOTS`            | 422  | more isolating intervals than budget allows|
| `BUDGET_EXCEEDED`           | 422  | exact-evaluation budget exhausted          |
| `INCONCLUSIVE`              | 409  | depth limit / cannot certify within budget |
| `EVIDENCE_MISMATCH`         | 409  | exact witnesses disagree (result withheld) |
| `INTERNAL_ERROR`            | 500  | unexpected fault                           |

Every error carries the `request_id`, the stable category, a human message, and
a redactable `state` object.

---

## 8. Diagnostics and sensitive data

* Every log line is structured JSON with the **request id**, an `event`, and
  only safe `state` (degree, term count, fingerprint, counts).
* Raw coefficient payloads are replaced by `<redacted>` by default
  (`RI_LOG_REDACT=false` to disable for a trusted local environment).
* The `fingerprint` is a stable, non-reversible descriptor
  (`deg=…;terms=…;hash64=…`); distinctive coefficient values never appear in it.
* Accept/reject/indeterminate decisions log *why*, with the key state.

---

## 9. How tests independently check the answer (not just "the API works")

The test suite never asks the kernel under test for the expected answer:

* **`tests/oracle.py`** — a from-scratch Sturm implementation over sparse
  `dict` polynomials (different representation and code path), used to verify
  sign-variation counts and global root counts.
* **Construction by roots** — polynomials are built as `∏ (x−r)^m`, so roots
  and multiplicities are known exactly by construction.
* **Hand-authored sign table** — `tests/fixtures/root_cases.json` contains the
  hand-computed Sturm signs for `x²−1` at `−2,−1,0,1,2`; the test recomputes
  variations from the *hand* signs and checks endpoint zeros.
* **Independent constants** — the `√2` intervals are compared against the
  published decimal expansion of `√2`, and the `10^40 x²−1` case against the
  known `±10^-20` constants.
* **Property tests** — 30 randomized polynomials assert unique half-open
  ownership, disjointness, multiplicity correctness, and oracle agreement.
* **Failure-category tests** assert the specific category and HTTP status for
  floats, malformed input, empty input, and every budget guard.
* **Redaction tests** assert a distinctive secret coefficient never reaches
  the logs while the request id does.

---

## 10. Verified status and remaining limitations

**Verified by actual execution** (see commit/test run): full suite green;
service started under uvicorn and exercised with real HTTP for repeated roots,
close roots, no-real-roots, zero polynomial, malformed input, float rejection,
and log redaction; the independent Descartes counter cross-checked on 960+
random half-open intervals including endpoint-root cases.

Remaining limitations, by design:

1. **Univariate polynomials over ℚ only.** No multivariate, transcendental, or
   interval-coefficient input.
2. **Complex roots are counted, not isolated.** Degree accounting gives the
   number of non-real roots (with multiplicity); no complex intervals are
   produced.
3. **Worst-case exact cost.** Sturm remainder coefficients can grow; the
   explicit budgets bound this, and pathological beyond-budget cases return
   `INCONCLUSIVE`/`BUDGET_EXCEEDED` rather than running unbounded.
4. **mpmath is a witness, not a decider.** In principle arbitrary-precision
   numeric root finding can fail to converge; such a failure is rendered
   `indeterminate`, never treated as proof.
5. **Float64 is intentionally weak.** It corroborates ordinary cases but
   reports `inconclusive` for sub-double-precision separations and out-of-range
   magnitudes; the exact witnesses still certify those answers.
6. **Single-process / local deployment.** Auth, rate limiting, and persistence
   are out of scope for this local, synthetic-data service; put it behind a
   gateway for any shared deployment.
