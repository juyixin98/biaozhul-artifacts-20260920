# Summation Comparison API

A Python / FastAPI backend that compares three families of floating-point
summation over large, possibly streaming sequences —

- **naive** left-to-right summation,
- **pairwise** divide-and-conquer summation,
- **compensated** Kahan summation (including two *genuinely stateful*
  chunked variants)

— against an **independent high-precision reference** (mpmath arbitrary
precision, cross-validated with CPython `math.fsum` and 80-bit long double),
with **explainable error evidence**: a-priori error bounds, measured errors,
condition numbers, and an explicit **accepted / rejected / inconclusive**
verdict for every method under several rearrangements.

## Why

Floating-point addition is not associative. For example
`[1e16, 1, 1, …, 1, -1e16]` sums to `0.0` with naive summation even though
the exact answer is the number of ones; Kahan compensation recovers all of
them. Blocked/streaming computation only preserves that accuracy when the
**compensation state crosses block boundaries** — summing per-block totals
does not. This service makes all of that measurable, not just asserted.

## Architecture

Real responsibilities are split into modules; this is not a single-file script.

```
app/
├── config.py                 # env-driven, validated, immutable settings
├── core/
│   ├── kernels.py            # naive / kahan / pairwise kernels + IEEE special policy
│   ├── chunking.py           # blocked algorithms, KahanState, error-free TwoSum / double-double merge
│   ├── errors.py             # a-priori bounds, mpmath/longdouble oracles, verdict logic
│   └── blas_utils.py         # scipy BLAS dasum (numpy fallback) for sum|x_i|
├── services/
│   ├── inputio.py            # boundary parsing/validation, NaN/Inf tokens, error categories
│   ├── synthetic.py          # deterministic stress scenarios (cancellation, eps tails, …)
│   ├── comparison.py         # orchestrates orderings × methods × chunked variants
│   └── diagnostics.py        # request ids, JSON logs, magnitude-bucket redaction
└── api/
    ├── schemas.py            # pydantic request models
    └── main.py               # FastAPI app, middleware, error envelope, routes
tests/                        # 200+ independent assertions (kernel, error, ordering, HTTP, boundary)
examples/
    └── call_api.py           # runnable client walkthrough
```

## Install (locked dependencies)

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt          # exact pinned versions
pip install -e .                         # optional: install the package itself
```

Python ≥ 3.10. Verified locally on Python 3.12 with the pinned versions in
`requirements.txt`.

## Run

```bash
python3 -m uvicorn app.api.main:app --host 127.0.0.1 --port 8000
# interactive docs: http://127.0.0.1:8000/docs
```

Configuration is environment-driven (see `app/config.py` for defaults):

| Variable | Default | Meaning |
|---|---|---|
| `SUMAPI_MAX_INPUT_LENGTH` | `5000000` | max elements per request |
| `SUMAPI_DEFAULT_BLOCK_SIZE` | `4096` | default chunk width |
| `SUMAPI_REFERENCE_PRECISION` | `80` | mpmath decimal digits |
| `SUMAPI_REFERENCE_MAX_LENGTH` | `200000` | above this, use the long-double oracle |
| `SUMAPI_ERROR_TOLERANCE_FACTOR` | `4.0` | verdict multiplier on the a-priori bound |

## Example calls

Raw values (special values use the tokens `NaN` / `Infinity` / `-Infinity`,
since JSON has no such literals):

```bash
curl -s -X POST http://127.0.0.1:8000/api/v1/compare \
  -H 'Content-Type: application/json' \
  -d '{"values":[1e16,1,1,1,1,1,1,1,1,1,1,-1e16],"block_size":4}' | python3 -m json.tool
```

Deterministic synthetic scenario:

```bash
curl -s -X POST http://127.0.0.1:8000/api/v1/scenario \
  -H 'Content-Type: application/json' \
  -d '{"scenario":"big_cancel","n":100002,"orderings":["original","abs_ascending","shuffle"],"shuffle_seed":3}'
```

Or run the bundled client:

```bash
python3 examples/call_api.py --base-url http://127.0.0.1:8000
```

See [`examples/RESULTS.md`](examples/RESULTS.md) for annotated sample output.

## Endpoints

- `GET  /health` — liveness + service/version/request id.
- `GET  /api/v1/info` — methods, orderings, scenarios, effective config.
- `POST /api/v1/compare` — body `{values, block_size?, orderings?, shuffle_seed?}`.
- `POST /api/v1/scenario` — body `{scenario, n, block_size?, orderings?, shuffle_seed?}`.

Every request/response and log line carries the same `request_id`
(a client may supply one via the `X-Request-Id` header). Errors use a
uniform envelope `{ok:false, error:{code,message,...}, request_id}` with a
specific failure `code` (e.g. `empty_input`, `input_too_large`,
`unparseable_token`, `non_numeric`, `bad_block_size`,
`schema_validation_error`).

## Fixed IEEE-754 special-value rules

The verdict on NaN/Inf is order-independent and fixed across all methods:

- any NaN present → quiet `NaN`;
- both `+Inf` and `-Inf` present → invalid operation, quiet `NaN`;
- only `+Inf` / only `-Inf` → signed infinity;
- empty set → `+0.0`; all-negative-zero input → `-0.0`; every other zero
  (mixed signs, or exact cancellation) → `+0.0`.

## What “error evidence” means

For each method/ordering the response reports the measured absolute error
versus the oracle, the relative error, the **a-priori bound**
(`γₖ·Σ|xᵢ|`, with `k=n-1` for naive, `O(log n)` for pairwise, and the
second-order `(2u+n·u²)/(1-2u)·Σ|xᵢ|` for Kahan), the observed/bound ratio,
and the condition number `Σ|xᵢ| / |Σxᵢ|`.

Verdicts are honest about oracle precision: the true error is treated as an
*interval* `[|e|-δ, |e|+δ]` where `δ` is the oracle's self-reported
uncertainty. If the interval is wholly inside `factor·bound` → **accepted**;
wholly beyond it → **rejected**; if it straddles the threshold →
**inconclusive** (rather than a rubber stamp). Severe cancellation
(condition near `1/u`) is annotated: no summation rule can recover digits
that are absent from the floating-point inputs themselves.

## Testing

```bash
python3 -m pytest                          # 200+ tests
python3 -m coverage run -m pytest && python3 -m coverage report   # ~93%
```

The reference answers in tests come from **mpmath, `math.fsum`, and long
double — never from the kernels under test** — and tests assert concrete
locked results and failure categories, not “the endpoint responded”.

## Known limitations

- The Naive/Pairwise kernels use Python-float loops (order guaranteed, not
  vectorised); throughput is bounded at ~5 M elements by design.
- Long-double precision (80-bit) is x86-specific; on platforms without it the
  large-input oracle is weaker and more results are correctly reported
  `inconclusive`.
- The double-double shard merge is compensated-order accurate but is **not**
  bit-identical to one monolithic Kahan pass; the response invariants state
  exactly which identities are bit-exact and which are error-order claims.
- NaN/Inf are rendered as the JSON strings `"NaN"`/`"Infinity"`/`"-Infinity"`
  (strict JSON has no such literals).
