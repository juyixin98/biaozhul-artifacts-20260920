# 2SLS Service — Synthetic Linear Models with Weak-Instrument Diagnostics

A self-contained FastAPI service that estimates **two-stage least squares (2SLS)**
for linear structural models on local synthetic data, with first-stage projection,
the **correct 2SLS standard errors**, multi-instrument rank/relevance checks,
weak-instrument diagnostics (Cragg-Donald / Stock-Yogo), over-identification and
endogeneity tests, and an **explained, request-correlated decision record**.

No external accounts or real data are required: every dataset is produced by a
seeded local data-generating process (`app/dgp.py`).

---

## 1. Module layout (real responsibilities, no single-file script)

```
app/
  contracts.py       statistical contract: request/response schemas, verdict &
                     failure enums, assumption records
  errors.py          stable domain error taxonomy (code + HTTP status + details)
  config.py          JSON config under config/, env-var overrides
  logging_context.py request correlation ids, payload fingerprinting, redaction
  data_prep.py       boundary validation -> numpy matrices (shapes, finite, size)
  kernel.py          estimation core (pure numerics, no I/O):
                     first stage, 2SLS, correct VCV, Cragg-Donald,
                     Sargan, two-step GMM/Hansen J, DWH, rank/VIF
  diagnostics.py     evidence assembly, error diagnostics, Stock-Yogo decision,
                     assumption records, decision record, failure classification
  dgp.py             reproducible synthetic data generator (fixtures/experiments)
  service.py         orchestration: validate -> estimate -> diagnose -> record
  storage.py         SQLite persistence of metadata/decisions (never raw data)
  api.py             thin FastAPI boundary, typed error envelope, request ids
  main.py            uvicorn entrypoint
scripts/
  demo.py                    local end-to-end demo (4 scenarios)
  reproduce_experiments.py   seeded Monte-Carlo reproduction
tests/
  oracle.py                          INDEPENDENT matrix-formula reference
  test_kernel_against_oracle.py      point estimates / SEs / tests vs oracle
  test_identification_failures.py    order, rank, weak, collinear, invalid IV
  test_validation.py                 boundary validation categories
  test_service_api.py                HTTP, persistence, correlation ids
  test_contract_semantics.py         exclusion is DECLARED, never inferred
  test_reproducible_experiments.py   endogeneity / weak / collinear experiments
  test_edge_paths.py                 marginal band, no-W/no-constant, config
  test_external_reference.py         OPTIONAL cross-check vs `linearmodels`
config/service.json
```

---

## 2. Statistical contract

**Model.** `y = W γ + X β + ε`, endogenous regressors `X` (n×K), included
exogenous controls `W` (n×J, constant prepended by default), excluded
instruments `Z` (n×L). Full instrument matrix `Zm = [W, Z]`, regressor matrix
`R = [W, X]`.

**Estimator.**
```
β_2SLS = (R′ P_Z R)^−1 R′ P_Z y,   P_Z = Zm(Zm′Zm)^−1 Zm′
```
Structural residuals use the **original** endogenous regressors:
`u = y − R β` (never `y − [W, X̂] β`).

**Variance — the point the brief calls out.** The service does **not** use the
naive second-stage OLS variance. Conventional VCV is

```
V̂ = σ² (R′ P_Z R)^−1,   σ² = u′u/(n − k)
```

and the robust variant is the 2SLS White sandwich

```
V̂_rob = (R′P_ZR)^−1 (R̂′ diag(u²) R̂) (R′P_ZR)^−1 · n/(n−k),  R̂ = P_Z R.
```

`tests/test_kernel_against_oracle.py::test_conventional_se_is_correct_2sls_formula_not_naive_second_stage`
asserts the service matches the correct formula and provably differs from the
naive formula (the two residual scales are shown unequal on endogenous data).

**Rank & relevance (testable).**

- Order condition: require `L ≥ K`, else `unidentified_model`.
- Rank of `[W, Z]` must be full (SVD, configurable `rank_rcond`), else
  `singular_design`.
- Rank of the projected first stage `M_W X̂ = M_W Z Π` must be K, else
  `unidentified_model` (`weak_identification`).
- Multi-instrument relevance: **Cragg-Donald Wald F** (minimum-eigenvalue
  form); with K=1 it is exactly the excluded-instruments partial F (asserted in
  tests). Cutoffs are **Stock & Yogo (2005) 10% maximal IV relative bias**
  critical values for tabulated (K,L) cells, else the Staiger–Stock rule of 10
  (labelled in the output as a rule-of-thumb cell). A 20% marginal band yields
  `accepted_with_warning`.
- Collinearity: per-instrument VIF on residualized instruments above
  `vif_warn` (default 30) is recorded as a warning even when rank survives.

**Other diagnostics.** Sargan score (homoskedastic over-id), Hansen J from an
independently coded two-step efficient GMM path (robust), Durbin–Wu–Hausman
augmented-regression endogeneity test, Breusch–Pagan heteroskedasticity score,
and residual distribution summary (skew/excess kurtosis/Jarque–Bera).

**Instrument validity — declared, never inferred.** The exclusion restriction
`E[Z′ε]=0` is **not** a consequence of instrument relevance or of high
instrument–regressor correlation. The request therefore **requires**
`assume_exclusion_restriction: true` plus an optional rationale, and the result
records it with status `declared` (or `not_assessed`). Only the rank/relevance
condition is ever marked `supported`. A non-rejected Sargan/Hansen test is
described as consistency evidence, not proof; under exact identification the
over-id test is undefined and is omitted (not faked).

---

## 3. Verdict and error semantics

Every response is a typed contract. Successful estimation always returns HTTP
200 — **weak instruments are not an HTTP error**: coefficients are still
reported with `status="estimated_weak"` and `decision.verdict="inconclusive"`.

Decision verdict: `accepted` / `accepted_with_warning` / `inconclusive`.
Hard failures are rejected at validation/estimation with a stable envelope:

```json
{"error": {"code": "unidentified_model", "message": "...",
           "details": {"n_excluded_instruments": 1, "n_endogenous": 2},
           "request_id": "..."}}
```

| code | HTTP | meaning |
|---|---|---|
| `invalid_request` | 422 | schema failure (missing field, bad type) |
| `incompatible_shapes` | 422 | missing column, column in two roles, unequal length, n > max |
| `insufficient_observations` | 422 | n smaller than regressors + residual df |
| `non_finite_data` | 422 | NaN/Inf in a named column group |
| `singular_design` | 422 | `[W, Z]` rank deficient (e.g. constant instrument) |
| `unidentified_model` | 422 | order condition (`L<K`) or rank condition failure |
| `inapplicable_test` | 422 | diagnostic undefined for the specification |
| `not_found` | 404 | unknown run id |
| `internal_error` | 500 | unexpected failure |

`decision.failure_category` repeats the statistical category on 200 responses:
`none`, `weak_instruments`, `collinear_instruments`, etc. Each decision carries
`request_id`, payload `fingerprint`, `key_state` (n, ranks, CD F, cutoff, VIF,
test p-values) and human-readable `reasons` stating **why** the verdict was
reached.

**Privacy.** Raw observations are never logged or persisted. Logs contain only
shapes, covariance type, and a truncated SHA-256 fingerprint of the payload;
SQLite stores metadata/decisions only (asserted in tests).

---

## 4. Running

```bash
pip install -r requirements.txt

# tests (actual execution; see section 6)
python3 -m pytest -v

# local demo (4 scenarios, prints redacted metadata + decisions)
python3 scripts/demo.py

# seeded Monte-Carlo reproduction
python3 scripts/reproduce_experiments.py --reps 200

# service
python3 -m app.main            # or: uvicorn app.main:app --reload
```

### Example request

```bash
curl -s localhost:8000/health
curl -s -X POST localhost:8000/api/v1/estimate \
  -H 'Content-Type: application/json' \
  -H 'X-Request-ID: demo-001' \
  -d '{
    "dependent": "y",
    "endogenous": ["x1"],
    "exogenous": ["w1"],
    "instruments": ["z1", "z2"],
    "cov_type": "conventional",
    "assume_exclusion_restriction": true,
    "exclusion_rationale": "synthetic DGP: Z generated independently of epsilon",
    "columns": {"y": [...], "x1": [...], "w1": [...], "z1": [...], "z2": [...]}
  }'
```

Endpoints: `GET /health`, `POST /api/v1/estimate`, `GET /api/v1/runs/{id}`,
`GET /api/v1/runs`. The effective request id is returned in the response body
and the `X-Request-ID` header (header wins, then body id, then a generated one).

Configuration lives in `config/service.json` and is overridable by environment
variables (`TWOSLS_MAX_OBS`, `TWOSLS_ALPHA`, `TWOSLS_DB_PATH`, `TWOSLS_COV`,
`TWOSLS_WEAK_RULE`, `TWOSLS_RANK_RCOND`, `TWOSLS_VIF_WARN`, …).

---

## 5. Reproduced experiments (seeded)

`python3 scripts/reproduce_experiments.py --reps 200` reports, approximately:

1. **Known endogeneity (ρ(v,e)=0.8):** mean OLS bias ≈ **+0.49**, mean 2SLS
   bias ≈ **0.00**, DWH rejection ≈ **100%** despite identical data.
2. **Weak instruments:** median Cragg-Donald F ≈ **1.1** (cutoff 19.93),
   flagged `inconclusive` ≈ **100%**, and 2SLS visibly biases toward OLS.
3. **Collinear instruments:** rank/order conditions survive but max VIF is
   ≈ 2×10⁸ and the result is `accepted_with_warning`.

A fourth demo scenario builds an **invalid instrument** (direct load on the
structural error): relevance is overwhelming yet Sargan rejects — demonstrating
that strength cannot establish exclusion.

---

## 6. Tests and independent validation

Answers are **not** generated by the code under test. `tests/oracle.py`
re-derives every benchmark from raw matrix primitives and imports nothing from
`app.kernel`: closed-form IV via an independently built projection matrix,
correct vs. naive second-stage variances, eigen-decomposition Cragg-Donald, a
separately written two-step GMM, Sargan and DWH regressions. Tests assert
concrete values (tolerances ~1e-8), concrete failure categories, bias
directions, and rejection rates — not merely that endpoints respond.

`tests/test_external_reference.py` additionally compares point estimates,
conventional and robust SEs, Sargan and Cragg-Donald against the mature
[`linearmodels`](https://pypi.org/project/linearmodels/) package; it **self-skips
when the package is absent**, so the suite remains fully self-contained. To run
it: `pip install linearmodels pandas && python3 -m pytest -m reference`.

```bash
python3 -m pytest                                  # full suite
python3 -m pytest --cov=app --cov-report=term      # coverage (>= 95%)
python3 -m pytest -m identification                # failure categories only
```

Statistical sanity checks beyond the suite: the Sargan rejection rate under
valid instruments matches nominal size (3% at 5% over 200 seeds, within Monte
Carlo noise), and K=1 Cragg-Donald equals the partial F to machine precision.
