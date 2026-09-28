# Symmetric Matrix Eigendecomposition Service

A multi-module Python backend that computes the eigendecomposition of a real
symmetric matrix from scratch and **independently proves the result**. The
numerical core is a Householder tridiagonalization followed by an implicit
Wilkinson-shift QR iteration with Givens bulge chasing — no eigenroutine is
called for the answer under test. Reference answers come from independent
mature libraries (SciPy/LAPACK and mpmath arbitrary precision).

Everything runs locally on deterministic synthetic data — no production
accounts or external services.

---

## 1. What it does

Given a real symmetric matrix `A`, returns eigenvalues `w` and orthonormal
eigenvectors `V` such that `A = V diag(w) Vᵀ`, together with:

* **Residual evidence** — `‖A V − V diag(w)‖`, both overall and per vector.
* **Orthogonality evidence** — `‖Vᵀ V − I‖`.
* **Reconstruction evidence** — `‖A − V diag(w) Vᵀ‖`.
* **Independent cross-checks** against mpmath high precision (small matrices)
  or SciPy/LAPACK `dsyevd` (large matrices).
* **Degenerate-eigenvalue handling** — repeated/near-degenerate eigenvalues
  are compared as **eigenspaces** (principal angles), never vector-by-vector.
* **Conditioning-aware limits** — when float64 input itself bounds the
  attainable accuracy (e.g. a tiny eigenvalue inside 1e8-scale entries), the
  bound is computed from the Weyl/Eckart–Davis–Kahan theory and reported in a
  separate `limitations` list, distinct from genuine uncertainties.

## 2. Architecture

```
sym_eig/
├── numerical/             # LAYER 1 — numeric input + compute kernel
│   ├── validation.py      #   shape/type/finiteness/size/symmetry checks
│   └── kernel.py          #   Householder tridiagonalization + implicit
│                          #   Wilkinson QR (Givens bulge chase)
├── evidence/              # LAYER 2 — independent proof of the result
│   ├── measures.py        #   residual / orthogonality / reconstruction
│   ├── subspace.py        #   eigenvalue clusters + principal-angle spaces
│   └── reference.py       #   mpmath (arbitrary precision) & SciPy oracles
├── service/               # LAYER 3 — orchestration, taxonomy, tracing
│   ├── engine.py          #   pipeline, evidence gates, verdict logic
│   └── tracing.py         #   correlation id, step trace, processing loc
└── api/                   # LAYER 4 — FastAPI HTTP interface
    ├── schemas.py
    └── app.py
scripts/
├── run_server.py          # uvicorn entry point
└── demo.py                # offline end-to-end demonstration
tests/                     # independent pytest suite (see §7)
```

The kernel never certifies itself: `kernel.py` contains no call to any
eigenroutine, and the verdict is decided entirely by the evidence layer.

### Algorithm outline

1. **Householder reduction** — orthogonal similarity reduction of the
   symmetric matrix to tridiagonal form `T = Qᵀ A Q`, accumulating `Q`.
2. **Implicit QR with Wilkinson shift** — the trailing unreduced block is
   shifted by the eigenvalue of its trailing 2×2 block nearest the corner; a
   bulge is created and chased down the band with Givens rotations, all
   accumulated into the eigenvector matrix.
3. **Deflation** — an eigenvalue is accepted *only* when its off-diagonal
   element falls below `tol·(|dᵢ|+|dᵢ₊₁|)`. Exhausting the configured sweep
   budget raises `NonConvergenceError`; stopping the loop is never treated as
   convergence.

## 3. Setup

```bash
python3 -m venv .venv && source .venv/bin/activate   # optional
pip install -r requirements.txt
```

Requires Python ≥ 3.11. The stack is NumPy, SciPy, mpmath, FastAPI, uvicorn,
pydantic v2, pytest, httpx.

## 4. Running the service

```bash
python scripts/run_server.py            # http://127.0.0.1:8000
# interactive docs: http://127.0.0.1:8000/docs
```

```bash
# health (version, algorithm, active config)
curl -s localhost:8000/health

# eigendecomposition (repeated eigenvalue here: [1,1,3])
curl -s -X POST localhost:8000/api/v1/eigendecomposition \
  -H 'content-type: application/json' \
  -H 'X-Request-ID: demo-42' \
  -d '{"matrix":[[2,1,0],[1,2,0],[0,0,3]]}'
```

## 5. Offline demo

```bash
python scripts/demo.py
```

Runs six synthetic cases and prints the full interpretable report:
diagonal, repeated spectrum, near-degenerate, widely separated scales, a
non-symmetric input, and an iteration-budget failure.

## 6. Configuration

All limits and tolerances are explicit. Per-request fields override the
server `Settings`; environment variables (`SYMEIG_*`) set server defaults.

| Setting                | Env var                      | Default | Meaning |
|------------------------|------------------------------|---------|---------|
| `max_n`                | `SYMEIG_MAX_N`               | 256     | dimension (size budget) |
| `max_iters`            | `SYMEIG_MAX_ITERS`           | 30      | per-block QR sweep budget |
| `hard_max_iters`       | `SYMEIG_HARD_MAX_ITERS`      | 1000    | ceiling a request cannot exceed |
| `symmetry_rtol/atol`   | `SYMEIG_SYMMETRY_RTOL` …     | 1e-10/1e-12 | symmetry gate |
| `residual_rtol`        | `SYMEIG_RESIDUAL_RTOL`       | 1e-9    | residual & eigenvalue-match gate |
| `orthogonality_tol`    | `SYMEIG_ORTHOGONALITY_TOL`   | 1e-9    | `VᵀV−I` and subspace gate |
| `reconstruction_rtol`  | `SYMEIG_RECONSTRUCTION_RTOL` | 1e-9    | reconstruction gate |
| `cluster_rtol`         | `SYMEIG_CLUSTER_RTOL`        | 1e-8    | relative gap defining a degenerate cluster |
| `reference_dps`        | `SYMEIG_REFERENCE_DPS`       | 50      | mpmath precision |
| `reference_max_n`      | `SYMEIG_REFERENCE_MAX_N`     | 24      | below this mpmath is used, else SciPy |

A request can lower the iteration budget to force the non-convergence path
(`"max_iters": 1`), and tighten any evidence gate to force `UNCERTAIN`.

## 7. Testing

```bash
python3 -m pytest                                  # all tests
python3 -m pytest --cov=sym_eig --cov-report=term # coverage (~99%)
```

The suite asserts **concrete results and failure categories**, not that an
endpoint can be called:

* `tests/test_kernel.py` — eigenvalues vs NumPy across sizes/scales, exact
  repeated spectra, Householder similarity, and that an exhausted budget
  raises rather than returns a fake result.
* `tests/test_validation.py` — every invalid-input category and the
  *relative* symmetry check (same absolute skew rejected at O(1), accepted at
  O(1e8)).
* `tests/test_evidence.py` — measures on exact/corrupted decompositions,
  cluster formation, eigenspace rotation invariance, stable principal angles,
  and rejection of incompatible cluster partitions.
* `tests/test_reference.py` — proves the oracles are independent of the kernel
  and reach 60-digit accuracy on an exact high-precision matrix.
* `tests/test_engine.py` — the required diagonal / repeated / near-degenerate
  / wide-scale cases vs SciPy **and** mpmath; failure classification; budget
  configurability; the `UNCERTAIN` path; request-id/trace/location.
* `tests/test_security.py` — overflow/out-of-range inputs never 500, body-size
  cap, request-id sanitization, parameter-vs-matrix taxonomy, and the rule
  that an indeterminate conditioning gate forces `UNCERTAIN`.
* `tests/test_api.py` — HTTP status codes, header/body correlation ids,
  success and classified failures over real ASGI requests.
* `tests/test_config.py` — env configuration and the status mapping table.

Reference answers in `tests/fixtures_cases.py` are generated by SciPy and
mpmath only — never by the implementation under test.

## 8. Result & error semantics

Every response carries `request_id`, `verdict`, `algorithm`,
`service_version`, `processing_location` (host, pid, Python/NumPy/SciPy/mpmath
versions), a timed `trace` of key steps, the `effective_config`, and the raw
`evidence` plus per-gate `value/threshold/passed`.

### Verdicts

| Verdict      | HTTP | Meaning |
|--------------|------|---------|
| `SUCCESS`    | 200  | kernel fully deflated **and** every evidence gate passed |
| `UNCERTAIN`  | 200  | a decomposition is returned, but at least one evidence gate failed; reasons are in `uncertainties` |
| `FAILED`     | 422  | no certified result; see `error_category` |

### Error categories (`error_category`)

| Category | Cause |
|----------|-------|
| `INVALID_MATRIX` | empty/ragged/non-square/non-numeric/NaN/Inf matrix content |
| `INVALID_PARAMETER` | a malformed request option/parameter (e.g. `max_iters` not a positive int) — distinct from bad matrix content |
| `NON_SYMMETRIC` | `‖A−Aᵀ‖∞` exceeds `atol + rtol·max(1,‖A‖∞)` |
| `SIZE_LIMIT_EXCEEDED` | dimension above `max_n`, or request body above the byte cap derived from `max_n` |
| `VALUE_OUT_OF_RANGE` | an entry is finite but too large for safe float64 arithmetic (e.g. `1e308`, or a Python integer beyond float64 range); rescale the matrix |
| `NON_CONVERGENCE` | QR sweep budget exhausted on an unreduced block |

Each gate also carries a machine `status`:

* `PASS` — measured value within the (possibly conditioning-adjusted) bound.
* `FAIL` — measured value exceeds the bound → `UNCERTAIN`.
* `SKIP` — the check is mathematically **indeterminate** (the conditioning
  bound is non-finite or ≥ `0.5`, so agreement would certify even a wrong
  eigenspace). A `SKIP` never contributes to success and forces `UNCERTAIN`.

`NON_CONVERGENCE` details include the unreduced block index range, sweeps
used, sweep budget, the remaining relative off-diagonal, and a suggestion.
**Stopping because the budget ran out is a `FAILED / NON_CONVERGENCE`, never a
success** with the partially converged diagonal. All malformed input —
including oversized JSON integers and finite-but-overflowing entries —
returns the same classified 422 envelope (never an opaque 500); request ids
are sanitized to `[A-Za-z0-9._-]` and length-capped to prevent log injection.

### `uncertainties` vs `limitations`

* `uncertainties` — gate failures that cast doubt on *this* answer
  (e.g. residual too large). The result is returned but marked `UNCERTAIN`.
* `limitations` — non-fatal statements about what the **input data** allows.
  A backward-stable decomposition can still disagree with another
  backward-stable solver on a tiny eigenvalue when the matrix has large norm;
  the discrepancy is bounded by `‖A‖_F·eps` (Weyl) for eigenvalues and by
  `‖A‖_F·eps/gap` (Davis–Kahan) for eigenspaces. These are reported as
  explanations, not failures.

## 9. Reproducing the key behaviors

```bash
# correct result vs independent libraries
python scripts/demo.py            # cases 1–4

# relative symmetry: same 1e-6 skew, different scale
python -m pytest tests/test_validation.py::test_symmetry_uses_relative_tolerance

# degenerate spectra compared by subspace (triple + double eigenvalues)
python -m pytest tests/test_engine.py::test_repeated_spectrum_compared_as_subspaces_not_vectors

# iteration stop is NOT success
python -m pytest tests/test_kernel.py::test_exhausted_budget_raises_and_does_not_pretend_convergence
python -m pytest tests/test_engine.py::test_stopping_the_iterations_is_not_reported_as_success

# configurable size/iteration budget
python -m pytest tests/test_engine.py::test_iteration_budget_configurable_changes_outcome

# ill conditioning reported separately, result still backward stable
python -m pytest tests/test_engine.py::test_widely_separated_scales_relative_measures_stay_small
```
