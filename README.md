# IPW-ATE — Cross-fitted IPW Average Treatment Effect with Overlap Diagnostics

Local, dependency-light implementation of **inverse-probability-weighted (IPW)
average treatment effects** with declared cross-fitting and an explicit
**overlap / positivity diagnostic** that *accepts, rejects, or cannot decide*.

Built only on Python + NumPy + SciPy, served through FastAPI, with SQLite for
an aggregate audit trail. No ML framework, no network services, no real data.

---

## 1. Statistical contract (what is being claimed)

| Item | Contract |
|---|---|
| Estimands | **ATE** (combined population) and **ATT** (treated population) |
| Weights | **Stable / Hájek**, normalized within each arm |
| ATE weights | treated `1/e(X)`, control `1/(1-e(X))` |
| ATT weights | treated `1`, control `e(X)/(1-e(X))` |
| Truncation | **Fixed, versioned**: scores clipped to `[0.01, 0.99]` before inversion (`fixed_ps_0.01_0.99_v1`) |
| Propensity | L2 logistic regression (SciPy L-BFGS-B), covariates standardized |
| Cross-fitting | Declared K-fold (default 5); each unit's score comes from a model trained **without** it |
| Boundary rule | A score exactly `0`/`1` or non-finite is **undefined → error**. There is **no silent epsilon denominator substitution** |
| Variance | Out-of-fold influence-function SE using the realized trimmed weights; normal 95% CI |
| Effect scale | Constant additive effect; synthetic truth `ATE = ATT = 2.0` |

### Decision rules

- **reject** — an arm is empty; a boundary/non-finite score; a *support void*
  (a tight cluster of units beyond the opposite arm's support on a covariate);
  per-arm effective sample size below the declared minimum (10); or
  **propensity-model misspecification** detected by weighted covariate balance
  (max balance z > 4 over an augmented basis).
- **inconclusive** — no hard failure, but interior scores are extreme
  (`<1e-3` / `>1-1e-3`), a normalized weight exceeds 10, an arm's ESS
  fraction is under 0.5, or the max balance z is between 3 and 4.
- **accept** — otherwise.

### Detecting propensity-model misspecification

IPW is robust to a misspecified *outcome* model but **not** to a misspecified
*propensity* model, and score calibration alone can miss a misspecification
orthogonal to the fitted linear index. The diagnostic therefore re-weights
and checks balance on an **augmented covariate basis** (raw covariates, their
squares, and pairwise interactions), reporting the largest standardized mean
difference as a z-statistic (`max_balance_z`). A correct linear propensity
stays below ~3 across sample sizes; an omitted quadratic drives it above 4
and is rejected. The `misspecified_propensity` scenario demonstrates this;
the `misspecified_outcome` control scenario confirms harmless outcome
non-linearity is not flagged.

### Assumptions (the estimate is not causal proof)

Every result carries these. The estimate is causal **only** under
unconfoundedness, positivity, and SUTVA, with an adequate propensity model.
A diagnostic verdict checks overlap/weighting; it does **not** prove a
causal effect or rule out unmeasured confounding.

---

## 2. Module responsibilities

```
src/ipw_ate/
  contract.py     statistical contract: configs, estimands, decision codes, records
  errors.py       classified failure hierarchy (specific .code per failure type)
  propensity.py   logistic model + LP separation check + declared K-fold cross-fit
  weights.py      stable Hájek weights, fixed truncation, Kish ESS
  estimator.py    arm means, ATE/ATT point estimate, influence-function SE/CI
  balance.py      weighted covariate-balance z over augmented basis (model fit)
  diagnostics.py  evidence packet + accept/reject/inconclusive rules + redacted log
  pipeline.py     boundary validation + end-to-end orchestration
  io_csv.py       CSV loader/validator
  synthetic.py    KNOWN generative processes (independent reference truth)
  storage.py      SQLite persistence of aggregate records only
  api.py          FastAPI service (/health, /analyze, /runs/{id})
tests/            independent tests incl. hand-computed weights/ESS/SE
scripts/          fixture generation, CSV CLI, reproducible experiment runner
examples/         live HTTP client (httpx) and curl examples
tests/fixtures/   minimal deterministic CSVs (known generative model)
results/          committed, reviewable experiment output
```

This is deliberately **not** a single-file script and not an interface-only
skeleton: the kernel, contract, diagnostics, and reproducibility layer each
have real, separately testable responsibilities.

---

## 3. Setup

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.lock.txt
pip install -e .          # or run with PYTHONPATH=src
```

---

## 4. Reproduce everything

```bash
# 1. Deterministic fixtures (known generative process)
PYTHONPATH=src python3 scripts/generate_fixtures.py

# 2. Test suite (independent assertions incl. hand calculations)
python3 -m pytest                       # add --cov for coverage

# 3. Reproducible scenarios -> results/experiments.{json,txt}
PYTHONPATH=src python3 scripts/run_experiments.py

# 4. CSV CLI on a normal and a failing fixture
PYTHONPATH=src python3 scripts/analyze_csv.py tests/fixtures/good_overlap.csv
PYTHONPATH=src python3 scripts/analyze_csv.py tests/fixtures/no_overlap.csv   # exit 2
```

### Observed reference run (committed in `results/`)

```
true ATE (by construction) = 2.0
good_overlap                 accept       tau=+1.923 SE=0.116 balz=1.93
support_void                 REJECT overlap_violation_error (no_overlap_cell)
extreme_interior_weights     inconclusive tau=+2.592 SE=0.162 balz=3.18
misspecified_outcome         accept       tau=+2.291 SE=0.185 balz=1.60
misspecified_propensity      REJECT overlap_violation_error (covariate_imbalance)
deterministic_separation     REJECT model_separation_error
```

Exact numbers depend only on the pinned seeds/configs.

---

## 5. Service example

```bash
IPW_RUNS_DB=data/runs.db PYTHONPATH=src \
  python3 -m uvicorn ipw_ate.api:app --port 8735

# another shell
IPW_BASE_URL=http://127.0.0.1:8735 PYTHONPATH=src python3 examples/call_service.py
BASE=http://127.0.0.1:8735 bash examples/curl_examples.sh
```

- `POST /analyze` body: `{treatment:[0/1...], outcome:[...], covariates:[[...]]}`,
  optional `estimand` (`ATE`/`ATT`), `n_splits`, `request_id`.
- `200` → estimate + diagnostic; `409` → overlap rejection with the evidence
  packet; `422` → classified data/model error; `GET /runs/{request_id}` → the
  persisted **aggregate** record.

---

## 6. How verification is independent of the code under test

- **Hand-computed small samples.** `tests/test_weights.py` and
  `tests/test_estimator.py` derive weights, Kish ESS, the point estimate and
  SE by hand from raw fractions and assert those exact numbers.
- **Known generative process.** Synthetic truth (`ATE=2.0`, true propensity)
  is fixed by construction in `synthetic.py`; the estimator is not used to
  manufacture reference answers.
- **Model mis-specification.** A non-linear outcome scenario checks the
  estimator still targets the known additive effect.
- **Overlap and separation.** A genuine support void and deterministic
  treatment are separate fixtures; tests assert the *specific* decision and
  failure code (`no_overlap_cell`, `model_separation_error`, …), not just
  that a function is callable. Heavy-tailed common-support data is asserted
  NOT to be falsely rejected.
- **Model misspecification.** A non-linear *propensity* mechanism (linear
  logistic omits a quadratic) is a separate fixture that must be rejected via
  `covariate_imbalance`; a non-linear *outcome* with correct propensity is the
  matched control that must still be accepted. This verifies the diagnostic
  separates the misspecification IPW is sensitive to from the one it is not.
- **Undefined cases.** Boundary scores, empty arms, single-arm folds, and
  zero-total-weight arms raise classified errors — never silent fallbacks.

---

## 7. Privacy / redaction

Logs and the SQLite store contain **only aggregates and the request id**
(counts, ESS, score extrema, reasons). Raw covariates and outcomes are never
logged or persisted.
