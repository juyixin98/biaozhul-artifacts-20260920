# Interval Root Certification Service

A service that **certifies** real roots of continuously differentiable
("restricted") expressions over a search interval, combining **interval
Newton** with **bisection**. A certified result is a mathematical proof — an
enclosure that provably contains exactly one root, with the theorem and its
evidence attached — not merely a numerical guess.

Floating-point approximations are computed by a **separate, independently
labelled** pipeline (SciPy) and are always marked `approximate_unverified`;
they are never confused with certified enclosures.

---

## What it does

Given `f(x)` and `[a, b]`, the service returns three disjoint kinds of result:

| Result | Meaning |
|---|---|
| `certified_roots` | Each has a **theorem-backed proof** that exactly one root lies in a tight enclosure. |
| `undecided_regions` | Sub-intervals where no theorem could be applied within budget (e.g. even-multiplicity/tangent roots), each with a concrete `reason`. |
| root-free (`status: "root_free"`) | The whole search interval is rigorously proved to contain **no** root. |

Overall `status` is one of `certified`, `root_free`, `partially_certified`,
`undecided`.

### Theorems used

All arithmetic is **outward-rounded interval arithmetic** (mpmath, 50 decimal
digits by default), so every computed range is a rigorous enclosure.

1. **Range exclusion** — if `0 ∉ f(X)`, then `X` contains no root.
2. **Interval-Newton exclusion** — with `N(X) = m(X) − f(m(X))/f'(X)`, if
   `X ∩ N(X) = ∅`, then `X` contains no root.
3. **Interval-Newton uniqueness** — if `X ∩ N(X) ⊂ int(X)`, then `X` contains
   **exactly one** root (the image intersection lies strictly inside `X`).
4. **Monotone intermediate-value theorem** — if `f'(X)` is strictly one-signed
   and the rigorous endpoint enclosures straddle zero (an endpoint value may be
   exactly zero), then `X` contains exactly one root. This is what certifies a
   root sitting **exactly on the search boundary**, where the strict-interior
   Newton condition cannot hold.

### Honouring the behavioural contracts

- **Containing zero is not evidence of a root.** `f(x)=x²−x` on `[0.1, 0.4]`
  has a natural interval range that straddles zero but no root in the interval;
  the service returns `root_free`. Uniqueness is asserted only when a theorem's
  hypotheses are verified.
- **Derivative enclosure crossing zero ⇒ conservative subdivision.** When
  `0 ∈ f'(X)`, extended interval division gives an unbounded Newton image that
  cannot contract, so the interval is bisected rather than guessed.
- **Multiple / no / undecided roots are reported separately.**
- **Value-range (domain) errors keep their source position.** `sqrt(x−1)` on
  `[0,2]` returns `DOMAIN_ERROR` with the character span of `x − 1`.

Even-multiplicity and tangent roots (e.g. `x²`, `(x−1)²`) **cannot** satisfy
the uniqueness theorems and are therefore returned as `undecided`
(`tangent_or_even_multiplicity`), never mis-certified.

---

## Supported expression grammar

Continuously differentiable, real-valued expressions in one variable `x`:

```
expr   := term (('+' | '-') term)*
term   := factor (('*' | '/') factor)*
factor := unary ('^' NUMBER)?       # exponent must be a numeric literal
unary  := ('+' | '-') unary | primary
primary:= NUMBER | 'pi' | 'e' | 'x' | FUNC '(' expr ')' | '(' expr ')'
FUNC   := sin | cos | tan | exp | log | sqrt
```

Numbers accept scientific notation (e.g. `1.5e-3`). Implicit multiplication is
rejected (`2x` → write `2*x`). Variable exponents are rejected; use `exp(...)`
for base-`e` variable exponents. `abs` is deliberately excluded (not
continuously differentiable). Interval bounds are exchanged as **decimal
strings** to avoid a binary-float round trip before certification.

### Key scope limitations (deliberate trade-offs)

- Single variable only; no multivariate or implicit equations.
- `log` is defined only on strictly positive arguments (an interval reaching
  zero is a domain error, matching the real-domain convention).
- `tan` across a pole yields infinite endpoint bounds and drives subdivision;
  it is handled, not hidden.
- Non-integer real powers require a non-negative base interval.
- Even-multiplicity roots are **isolated as undecided regions**, not certified
  as unique roots, because uniqueness theorems genuinely do not apply.
- The independent approximation layer uses a sign-change scan, so
  even-multiplicity roots are invisible to it too (they are covered by the
  certifier's undecided regions instead).

---

## Project layout

```
app/
  core/                 # computation kernel (no HTTP)
    ast.py              # expression AST nodes (with source spans)
    parser.py           # tokenizer + recursive-descent parser
    derivative.py       # symbolic differentiation
    evaluator.py        # interval & point evaluation, domain-error localisation
    interval_utils.py   # interval ops + outward-rounded decimal formatting
    certifier.py        # interval-Newton + bisection certification
    approximation.py    # independent, UNVERIFIED SciPy root search
    config.py           # budgets/precision (fail-fast on conflicts)
    errors.py           # stable error taxonomy (5 categories)
    tracer.py           # structured JSONL run logs for replay
  services/
    certify_service.py  # orchestration + JSON-safe serialisation
  api/
    schemas.py          # request/response/error Pydantic models
    routes.py           # thin FastAPI router
    app.py              # create_app() + error-envelope handlers
  main.py               # ASGI entry point
tests/                  # 113 tests, independent reference oracle in conftest.py
```

The design keeps four explicit boundaries — numeric input, computation kernel,
error evidence, and the service interface — with data/error contracts defined
between them.

---

## Quick start

Requirements: Python 3.12. A fully resolved, locally verified lockfile is
provided.

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.lock          # or: pip install -r requirements.txt

# run the service (logs to ./logs by default)
RC_LOG_DIR=./logs python3 -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

OpenAPI docs are at `http://127.0.0.1:8000/docs`.

### Example request

```bash
curl -s -X POST http://127.0.0.1:8000/certify \
  -H 'Content-Type: application/json' \
  -d '{"expression":"x^2 - 2","lower":"1","upper":"2","include_approximation":false}'
```

Response (abridged):

```json
{
  "expression": "x^2 - 2",
  "status": "certified",
  "certified_roots": [
    {
      "enclosure": {
        "lower": "1.414213562373095048801688724209698078560",
        "upper": "1.414213562373095048801688724209698078579"
      },
      "midpoint": "1.414213562373095048801688724209698078570",
      "certification": {"theorem": "interval_newton_uniqueness", "status": "certified"},
      "evidence": {
        "kind": "interval_newton_uniqueness",
        "midpoint": "1.5",
        "f_midpoint_enclosure": ["0.25", "0.25"],
        "derivative_range": ["2.0", "4.0"],
        "newton_image": ["1.375", "1.4375"],
        "search_interval": ["1.0", "2.0"],
        "image_intersection": ["1.375", "1.4375"],
        "containment": "intersection_strictly_inside_search_interval"
      },
      "residual_range_enclosure": {"lower": "...", "upper": "..."}
    }
  ],
  "undecided_regions": [],
  "summary": {"certified_root_count": 1, "root_free": false, "bisections": 0, "...": "..."},
  "trace": {"run_id": "20260928T...-........", "events": ["..."]}
}
```

The decimal `enclosure` is **outward-rounded and re-verified to contain** the
internal high-precision range, so a decimal consumer cannot step outside the
certified bounds.

More ready-made requests: [`examples/requests.sh`](examples/requests.sh).
Raw responses captured from a live server: `test-results/examples/`.

---

## Error taxonomy

Every failure has a stable `code` and one of five `category` values:

| Category | Example codes | HTTP |
|---|---|---|
| `input` | `PARSE_ERROR`, `INVALID_NUMBER`, `EXPRESSION_TOO_LARGE`, `REQUEST_VALIDATION` | 400 |
| `state_conflict` | `CONFLICTING_PARAMETERS` (e.g. `lower >= upper`) | 409 |
| `domain` | `DOMAIN_ERROR` (expression leaves the real domain; includes source position) | 422 |
| `resource` | `RESOURCE_EXHAUSTED` (evaluation/depth budget) | **200** with partial results |
| `computation` | `COMPUTATION_FAILED` (unexpected numerical failure) | 500 |

Resource exhaustion is intentionally **not** an HTTP error: the response
carries every root certified so far plus the still-pending regions, so partial
results remain usable and honest.

### Configuration / budgets

| Field / env var | Default | Meaning |
|---|---|---|
| `precision_dps` / `RC_PRECISION_DPS` | 50 | working decimal digits |
| `target_width` / `RC_TARGET_WIDTH` | 1e-20 | enclosure stopping width |
| `max_depth` / `RC_MAX_DEPTH` | 90 | max bisection depth per branch |
| `max_evals` / `RC_MAX_EVALS` | 200000 | max interval evaluations per run |
| `max_expression_len` / `RC_MAX_EXPRESSION_LEN` | 2000 | reject-before-parse length cap |
| `RC_LOG_DIR` | `./logs` | JSONL run-log directory |

Contradictory limits fail fast (e.g. display precision ≥ working precision).

---

## Replayable test logs

Each run gets a `run_id` (`YYYYMMDDTHHMMSS-xxxxxxxx`) and writes a JSONL log
(`logs/run-<run_id>.jsonl`). Every decision point records the stage, a
machine-readable `reason`, and the key intermediate state (interval bounds,
value/derivative ranges, Newton image, bisection children). The response
includes the first events in `trace.events`; the disk file holds **all** events
for full replay. Example event reasons: `accepted`, `range_excludes_zero`,
`newton_image_disjoint`, `derivative_crosses_zero_or_stall`,
`tangent_or_even_multiplicity`, `max_evaluations_reached`.

---

## Testing

```bash
python3 -m pytest                                   # all 113 tests
python3 -m pytest -m kernel                         # certification scenarios
python3 -m pytest -m reference                      # independent-oracle tests
python3 -m pytest --cov=app --cov-report=term-missing

# Independent soundness fuzz (mpmath.polyroots ground truth at 80 digits):
python3 scripts/fuzz_soundness.py 120
```

Coverage: **93%** overall (see `test-results/coverage.xml`,
`test-results/htmlcov/index.html`, JUnit `test-results/junit.xml`). The
randomized fuzz (120 degree-1..6 polynomials with close/simple roots) reports
**0 soundness violations**: every certified enclosure contains exactly one
independently computed root and certified counts match the independent
real-root count. Log: `test-results/fuzz-soundness.log`.

### How the tests avoid "the interface just runs"

- Tests assert **concrete classifications, exact theorems, and numeric
  enclosures**, not merely that a call succeeds.
- **Reference answers are independent of the kernel under test**
  (`tests/conftest.py`): ground-truth constants are computed with the standard
  library `decimal` (Chudnovsky for π, atanh series for ln 2, Taylor for e,
  Newton for √2). Polynomials are built from roots fixed *a priori*, expanded
  with NumPy, and only the expanded coefficient form is handed to the
  certifier. Root counts are additionally cross-checked with `numpy.roots` and
  a dense independent sign-change scan.
- Every certified enclosure is checked to contain the independently known root
  and the theorem hypotheses are independently re-derived from the recorded
  evidence.
- Scenarios explicitly cover simple roots, repeated/even-multiplicity roots,
  no-root intervals, very-near-root boundaries, roots exactly on a boundary,
  multiple close roots, all four/five error categories, and budget exhaustion.

---

## Verification status

The suite was executed locally (`113 passed`, 0 failures, 0 skipped), the
server was started with uvicorn and exercised with real `curl` requests (raw
outputs in `test-results/examples/`), and the replay logs were confirmed on
disk. An independent 120-case soundness fuzz produced 0 false/duplicate
certifications. There were no skipped or unexecuted tests at delivery time.
