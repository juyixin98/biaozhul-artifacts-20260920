# Mixed-Precision Iterative-Refinement Linear Solver

Solves `A X = B` using a **low-precision factorization + high-precision
residual + iterative refinement** strategy, exposed as a small FastAPI
service with explicit numerical evidence.

The residual is **always formed with the original matrix `A`** at a fixed
high decimal precision; the low-precision factors are used only to compute
corrections. When corrections stop improving a right-hand-side column, the
solver **escalates factorization precision**; if the configured precision
ladder is exhausted it reports **not met** — it never repeats or fakes a
convergence claim.

---

## 1. What it does

For each right-hand-side **column independently**, the engine:

1. Probes rank / singularity with a high-precision partial-pivot LU and, for
   small matrices, a high-precision SVD.
2. Estimates the condition number `cond(A)` (Hager/Higham 1-norm inverse
   estimator, corroborated by a 2-norm SVD) and states the attainable
   accuracy.
3. Factors `A` on an explicit precision ladder and refines:
   `x <- x + solve(r)` with `r = b - A x` evaluated at **60 decimal digits**
   on the **original** `A`.
4. Measures the **normwise relative backward error**
   `η = ‖r‖∞ / (‖A‖∞‖x‖∞ + ‖b‖∞)` and the **componentwise (Oettli–Prager)**
   backward error, and accepts a column only when a measured residual meets
   tolerance.

### Precision ladder (each stage explicit)

| stage        | factorization                 | correction precision |
|--------------|-------------------------------|----------------------|
| `float32`    | SciPy LU (`scipy.linalg`)     | ~7 decimal digits    |
| `float64`    | SciPy LU                      | ~16 decimal digits   |
| `mp-30/60/100` | pure-mpmath partial-pivot LU | 30 / 60 / 100 digits |

The residual and the iterates are kept in mpmath at `residual_dps = 60`,
which sets a backward-error floor of roughly `1e-59`. Asking for a tighter
tolerance than the floor returns `not_met` and names the floor.

### When refinement beats a direct low-precision solve

* **Well conditioned / `cond·u32 ≪ 1`** — refining from a float32 factor
  drives the backward error from `~1e-8` (raw float32) down to the residual
  floor, i.e. several orders of magnitude better than the direct solve.
* **Around `cond ≈ 1/u32 ≈ 8e6`** — float32 refinement saturates; the engine
  detects stagnation and **escalates to float64 / arbitrary precision**.
* **Very ill conditioned** — backward error can still be made tiny (the
  refined `x` solves a nearby problem very accurately), but the **forward
  error is bounded by `cond(A)·η`** and that bound is reported. The service
  does not imply forward accuracy it cannot justify.

---

## 2. Repository layout

```
app/
  config.py              # loads config/defaults.json, per-request overrides
  logging_setup.py       # request-id correlation + payload redaction
  input_parsing.py       # shape validation, exact decimal-string parsing
  schemas.py             # Pydantic request/response models
  service.py             # orchestration (thin business layer)
  api/
    __init__.py          # create_app() factory
    routes.py            # thin HTTP routers
  numerical/
    __init__.py          # mpmath contexts, unit roundoff, exact scalar parse
    arithmetic.py        # pure-mpmath matmul, norms, residual r = b - Ax
    factorization.py     # float32/64 SciPy LU + arbitrary-precision mp LU
    condition.py         # cond estimator (xLACON-style) + high-precision SVD
    error_metrics.py     # normwise & componentwise backward error per column
    engine.py            # staged refinement driver
    results.py           # accept/reject decisions + serializable evidence
config/defaults.json     # the exact precision ladder / tolerances
examples/                # sample requests
scripts/demo.py          # CLI comparison: direct low precision vs refined
tests/                   # independent reference + categorized assertions
```

The design deliberately separates **numeric input**, the **compute kernel**,
**error evidence**, and the **service interface** into modules with real
responsibilities; tests and configuration live in their own trees.

---

## 3. Setup (clean directory)

Requirements (pinned in `requirements.txt`, Python 3.12.3):

```
numpy==2.4.6   scipy==1.15.3   mpmath==1.3.0   fastapi==0.141.1
pydantic==2.13.5   uvicorn==0.54.0   pytest==9.1.1   httpx==0.28.1
```

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

---

## 4. Running

### CLI demonstration (no server)

```bash
python scripts/demo.py
```

### HTTP service

```bash
python -m uvicorn app.api:app --host 127.0.0.1 --port 8000
# openAPI docs: http://127.0.0.1:8000/docs
```

#### Example: well-conditioned

```bash
curl -s -X POST http://127.0.0.1:8000/solve \
  -H 'Content-Type: application/json' \
  -H 'X-Request-ID: req-42' \
  --data @examples/well_conditioned.json
```

#### Example: ill-conditioned batch (two RHS columns, >16-digit entries)

```bash
curl -s -X POST http://127.0.0.1:8000/solve \
  -H 'Content-Type: application/json' \
  --data @examples/ill_conditioned_batch.json
```

JSON numbers only carry float64 precision. Send matrix/RHS entries as
**strings** (`"1.00000000010000000000"`) to keep more than ~16 digits.

#### Example: singular matrix → HTTP 422, no solution

```bash
curl -s -X POST http://127.0.0.1:8000/solve \
  -H 'Content-Type: application/json' \
  --data @examples/singular.json
```

### Response categories and HTTP status

| status                   | HTTP | meaning                                            |
|--------------------------|------|----------------------------------------------------|
| `accepted`               | 200  | every column met the backward tolerance            |
| `partially_accepted`     | 200  | some columns met; each reported independently      |
| `not_met`                | 422  | solvable but tolerance unreachable / ladder exhausted |
| `singular_inconclusive`  | 422  | rank-deficient; no unique solution attempted       |
| `invalid_request`        | 400  | malformed payload / bad config override            |

Per-request overrides go under `"config"` (only an allow-list is accepted,
e.g. `backward_tol`, `mp_dps_ladder`, `use_fp32_first`,
`max_iterations_per_stage`, `solution_output_dps`).

---

## 5. Configuration

`config/defaults.json` (select another file with `MIPSOLVER_CONFIG=...`):

| key | default | meaning |
|-----|---------|---------|
| `residual_dps` | 60 | decimal digits for residual formation (sets the η floor) |
| `backward_tol` | `1e-12` | accept threshold for normwise backward error |
| `forward_tol` | `1e-9` | advisory forward-accuracy threshold (`cond·η`) |
| `max_iterations_per_stage` | 25 | refinement iterations before a stage is budget-exhausted |
| `stagnation_factor` | `0.8` | η not shrinking by this factor ⇒ stagnation count |
| `use_fp32_first` / `use_fp64` | true | enable binary stages |
| `mp_dps_ladder` | `[30,60,100]` | arbitrary-precision rungs |
| `rank_probe_dps` | 120 | precision of the rank/singularity probe |
| `svd_max_n` | 200 | use high-precision SVD corroboration only up to this n |
| `solution_output_dps` | 30 | digits printed in the returned solution |

---

## 6. Diagnostics and sensitive data

* Every log line carries the request correlation id (echoed from the
  `X-Request-ID` header, or generated as `req-<hex>`). Pass it with a request
  to correlate all records for that solve.
* Logs state **why** a stage / column was accepted, rejected, or judged
  inconclusive (measured η, stage, iteration, stagnation, precision floor).
* Matrix and RHS **entries are never logged** — only shapes and aggregate
  infinity norms appear (`tests/test_api_integration.py` asserts a rare
  coefficient cannot be found in the logs).

---

## 7. Verification process and recorded results

Tests are independent of the engine's own metrics. Reference answers come
from a **different code path**: mpmath's built-in dense `lu_solve` at
150 dps and exact rational Gaussian elimination with `fractions.Fraction`
(no floating point at all). Backward/forward errors in the tests are
recomputed from scratch in `tests/independent_checks.py`.

The retained validation covers:

* **Well-conditioned** — independently measured η ≤ tolerance and agreement
  with the 150-dps reference; refinement beats raw float32 by >1000×.
* **Solvable ill-conditioned (`cond ~ 1e10`)** — float32 refinement
  saturates, the ladder escalates to float64, η ≤ tolerance, and the forward
  error sits in the predicted `cond·η ≈ 1e-2` band rather than being claimed
  exact.
* **Refinement regime boundary** — at `cond ~ 1e4` fp32 refinement improves
  the forward error >1000× over direct fp32; at `cond ~ 1e8` fp32-only
  refinement is correctly reported `not_met` (no false convergence).
* **Singular** — the exact rank-2/3 integer matrix is rejected with rank
  evidence and returns no solution.
* **Unreachable tolerance** — asking `1e-150` with a 60-digit residual
  returns `not_met`, names the residual-precision floor, and does not claim
  convergence.
* **Per-column reporting** — two RHS aligned to the largest/smallest
  singular vectors converge at different rates under a fixed budget; one is
  accepted and one rejected in the same batch, reported independently.
* **Exact rational reference** — solutions agree with `Fraction`
  elimination; input matrices are verified unmutated; input validation
  asserts concrete failure categories (non-square, length mismatch, bad
  entry with coordinates, oversized dimension).

Run:

```bash
python -m pytest -q
```

**Recorded result on this machine (Python 3.12.3, Linux x86_64):**

```
48 passed in ~2 s      (package statement coverage 96%)
```

CLI demonstration (`python scripts/demo.py`), recorded output:

```
1. WELL-CONDITIONED (n=6, cond ~ 5)
  direct float32 eta : 1.095e-08
  direct float64 eta : 1.583e-17
  refined eta        : 6.412e-16   (accepted at float32 iter 2)
  refined forward error vs 150-dps ref: 1.983e-15

2. SOLVABLE ILL-CONDITIONED (n=8, cond ~ 1e10)
  log10 cond         : 10.444
  direct float32 forward error : 2.208e+00
  direct float64 forward error : 7.926e-08
  refined forward error        : 5.943e-08   (accepted at float64)
  refined eta        : 4.014e-17
  -> fp32 refinement saturates; ladder escalates to float64.

3. EXACT SINGULAR (rank 2/3)
  singular_inconclusive: rank 2/3: exact zero pivot in high-precision LU;
                          smallest singular value ~ 4.79e-122; solution None

4. UNREACHABLE TOLERANCE (cond ~ 1e20, tol 1e-150)
  not_met: eta 1.8e-62 is at the residual precision floor ~1e-57 (60 dps);
           raise residual_dps to go tighter. No convergence reported.
```

(Exact last digits can vary slightly with the BLAS/LAPACK build; the tests
assert bands and failure categories with margin, not last-digit literals.)

---

## 8. Scope notes

* Real matrices, square `A` (`n ≤ 256`), up to 32 RHS columns.
* Singular systems are diagnosed, not silently solved as least squares.
* All inputs and "external participants" are local synthetic fixtures; no
  production accounts or real business data are used.
