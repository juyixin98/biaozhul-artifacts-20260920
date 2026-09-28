# Sparse SPD Cholesky Backend

Symbolic and numeric factorization backend for **sparse symmetric positive
definite (SPD) matrices**. It computes the elimination tree and exact fill
structure *before* any numeric work, factors as `A = L D Lᵀ`, localizes the
offending pivot when the matrix is not positive definite, reuses a symbolic
structure only when the sparsity pattern is identical, and never densifies
the sparse input.

A FastAPI service, run-correlated JSONL diagnostics, and an independent test
layer are included.

---

## 1. What it does

Pipeline (see `sparse_cholesky/core/engine.py`):

1. **Ordering** — `natural` or reverse Cuthill–McKee (`rcm`, via SciPy).
   The permutation is applied **symmetrically** to the matrix and the RHS is
   permuted together with it (`B = P A Pᵀ`, `b_perm = P b`); the solution is
   mapped back with `x = Pᵀ y`.
2. **Elimination tree** — Liu (1986) path-compression etree, purely structural.
3. **Symbolic factorization** — exact column patterns of the unit-lower
   factor `L`, including fill, in `O(nnz(L))` total work using row-list
   threading. No numeric values are read.
4. **Numeric factorization** — left-looking sparse `L D Lᵀ` that visits only
   predicted nonzeros. Each pivot is checked; a non-positive pivot raises a
   categorized error carrying the permuted index **and** the original index.
5. **Solve** — sparse forward / diagonal / back substitution.
6. **Evidence** — sparse residual `b − A x`, sparse reconstruction `L D Lᵀ − A`,
   factor-pattern check, and an independent high-precision `mpmath` oracle for
   small matrices.

### Algorithm assumptions

- Input is the **lower triangle** (`row >= col`) of a symmetric matrix in COO
  form; the upper triangle is the mirror image.
- Every index has a **diagonal slot**. A present-but-zero (or negative)
  diagonal is accepted as input and later reported as the localized
  non-positive pivot — it is an SPD failure, not malformed input.
- Factorization is `A = L D Lᵀ` with a **unit** lower triangle and diagonal
  `D`; the conventional `A = L_c L_cᵀ` factor (`L_c = L √D`) is available.
- No pivoting is performed (valid for SPD matrices). A positive pivot below
  the configured tolerance is reported as a distinct `pivot_too_small_error`.
- Symbolic reuse is valid **iff** the sorted lower-triangular support is
  byte-identical; values may differ, structure may not.
- Orderings provided are `natural` and `rcm`. RCM is structural (values are
  ignored). On the grid fixtures it reduces both fill and factor time.

### Performance note (stated honestly)

The numeric kernel uses a Python dictionary as its sparse column accumulator
and pure-Python loops, prioritizing clarity and exact structural traversal.
It is comfortably fast on the bundled fixtures (n up to ~10k sparse in tens
of seconds; `scripts/verify.py` cases are sub-second) but is not a
compile-optimized supernodal CHOLAMD-style kernel. The symbolic phase, etree,
ordering, solve, and evidence paths operate directly on SciPy sparse index
arrays.

---

## 2. Project layout

```
config/
  settings.py                 tunables + env overrides (SPCHOL_*), no core deps
sparse_cholesky/
  input/
    matrix.py                 validated sparse container (lower-COO -> CSR/CSC)
    fixtures.py               local synthetic matrices (grid, banded,
                              dense block, non-PD, pattern-change pair)
    errors.py                 typed, categorized errors
  core/
    ordering.py               natural / RCM; symmetric matrix + RHS permutation
    etree.py                  elimination tree, height, postorder
    symbolic.py               exact fill structure (SymbolicFactor)
    numeric.py                left-looking sparse L D Lᵀ + pivot checks
    solve.py                  sparse forward / diagonal / back substitution
    cache.py                  exact-pattern symbolic cache + fingerprint
    engine.py                 end-to-end orchestration
  evidence/
    residual.py               sparse residual + reconstruction evidence
    high_precision.py         independent mpmath high-precision oracle
  api/
    schemas.py                Pydantic request/response models
    service.py                framework-free orchestration + failure mapping
    logging_setup.py          run_id JSONL logger + dependency versions
    app.py                    FastAPI app (/health, /symbolic, /factorize, /solve)
tests/                        independent pytest layer (dense references)
scripts/verify.py             end-to-end local verification harness
logs/                         JSONL run logs (created at runtime)
```

Dependency direction is one-way: `input` ← `core` ← `evidence`/`api`; `config`
depends on nothing in the package.

---

## 3. Setup

Python 3.12 and these versions are used and pinned:

| Package  | Version  |
|----------|----------|
| numpy    | 2.4.6    |
| scipy    | 1.15.3   |
| mpmath   | 1.3.0    |
| fastapi  | 0.141.1  |
| uvicorn  | 0.54.0   |
| pydantic | 2.13.5   |
| pytest   | 9.1.1    |
| pytest-cov | 7.1.0  |
| httpx    | 0.28.1   |

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
# the project uses src-layout-free imports; run commands from the repo root
```

---

## 4. Local verification

### Unit + integration tests (independent references)

```bash
python3 -m pytest
```

Expected: all tests pass with overall coverage ≥ 80% (currently ~93%).
Tests assert **concrete results and failure classes**, not "endpoint callable":

- solutions compared against an independent dense NumPy Cholesky solve;
- symbolic fill compared element-by-element against an independent dense
  boolean elimination oracle;
- pivots compared against an independent dense LDLᵀ pivot sequence;
- non-PD fixtures must raise `non_positive_definite_error` at the pivot index
  the dense reference predicts;
- pattern change must not reuse cached structure (explicit mismatch error);
- mpmath high-precision oracle agreement;
- HTTP failures return categorized `422` bodies, never a success envelope.

### End-to-end harness

```bash
python3 scripts/verify.py            # add --quick for the small set
```

Expected: every line is `[PASS]`, ending with `ALL CHECKS PASSED`, and JSONL
logs are written under `logs/`. It reports forward error, residual, fill
count/ratio, bandwidth before→after, cache behavior, and the localized pivot
for each non-PD fixture.

### Live service

```bash
uvicorn sparse_cholesky.api.app:app --host 127.0.0.1 --port 8000
curl http://127.0.0.1:8000/health
```

`POST /api/v1/solve` body (lower-triangle COO):

```json
{
  "matrix": {"n": 3, "entries": [
    {"row": 0, "col": 0, "value": 4},
    {"row": 1, "col": 0, "value": -1},
    {"row": 1, "col": 1, "value": 4},
    {"row": 2, "col": 1, "value": -1},
    {"row": 2, "col": 2, "value": 4}]},
  "rhs": [1, 2, 3],
  "ordering": "rcm",
  "run_id": "run-demo-0001"
}
```

Endpoints: `GET /health`, `POST /api/v1/symbolic`, `POST /api/v1/factorize`,
`POST /api/v1/solve`. Errors return HTTP 422 with `success:false`,
`error_type`, `pivot_index`, `pivot_value`, `original_pivot_index`, and the
same `run_id`.

### Diagnostics

Each run emits one JSON object per line keyed by `run_id`, including the
dependency versions at start, every pipeline step/progress event, the pivot
sequence, evidence values with their criterion, and a terminal
`succeeded`/`failed` status carrying the concrete error category. Exceptions
and unknown states are never collapsed into success.

---

## 5. Failure categories

| `error_type`                          | Meaning                                    |
|---------------------------------------|--------------------------------------------|
| `matrix_shape_error`                  | bad order/lengths/index/missing diag slot  |
| `off_diagonal_lower_error`            | an entry with `row < col` was supplied     |
| `duplicate_entry_error`               | repeated `(row, col)`                      |
| `matrix_not_finite_error`             | NaN / Inf value                            |
| `non_positive_definite_error`         | pivot `<= 0` (or non-finite); index given  |
| `pivot_too_small_error`               | positive pivot below reliability tolerance |
| `symbolic_structure_mismatch_error`   | reuse attempted with a different pattern   |

## 6. Test status

All tests are run locally; see section 4 for the commands and expected
results. Any test that cannot run in an environment is reported as such
rather than silently skipped.
