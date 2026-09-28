# Annotated example results

Captured live against `uvicorn app.api.main:app` using
`python3 examples/call_api.py`. Numbers are deterministic (IEEE-754
round-to-nearest; fixed seeds for shuffles).

## 1. Large-number cancellation

Input `[1e16, 1 ×100000, -1e16]`, block size 4096. Reference (mpmath, 80
digits) = `100000.0`.

| ordering | naive | kahan | pairwise |
|---|---|---|---|
| original | **0.0** (loses every 1) | **100000.0** | 99974 |
| abs_ascending | 100000.0 | 100000.0 | 100001 |

- Naive in the original order loses all 100000 ones because `1 < ulp(1e16)`;
  the huge a-priori bound still "accepts" this, but the response annotates a
  condition number ~`2e15` and explains the error as problem conditioning.
- Sorting small-first lets naive keep the ones — rearrangement matters.
- Pairwise is **not** Kahan: it loses a bounded 26 ones.

Chunked merge on the same block boundaries:

| strategy | abs error |
|---|---|
| `naive_sharded` (add per-block totals) | **4096.0** |
| `kahan_merged` (carry double-double compensation) | **1.0** |

This is the central streaming result: throwing away block-local compensation
costs thousands of units; carrying the `(hi, lo)` state across blocks keeps
the error at compensated order. Structural invariants reported by the API:
`blocked_naive == naive`, `kahan_streaming == monolithic kahan` (both
bit-exact), and `kahan_merged` within the compensated bound.

## 2. Small-number accumulation

Input `[1.0, 2^-53 × 100]`. Each `2^-53` is half the ulp near 1.0.

- naive → `1.0` (rounds every tiny term away),
- kahan → `1.000000000000011` = the reference (`50` ulps of 1.0).

## 3. Cancellation *of partial sums* (alternating harmonic, n=200000)

| ordering | naive absolute error |
|---|---|
| original | 3.797e-14 |
| abs_ascending | 1.110e-16 |

A ~340× improvement purely from accumulation order, with the same multiset
of inputs.

## 4. Fixed IEEE-754 policy

- `[1, +Infinity, -Infinity]` → invalid operation → quiet `NaN` (rendered as
  the JSON string `"NaN"`).
- `[-0.0, -0.0]` → `-0.0`; mixed zero signs or exact finite cancellation →
  `+0.0`.

## 5. Diagnostics and error categories

A malformed summand returns HTTP 422 with a typed envelope, never a raw
stack trace:

```
code = schema_validation_error
detail[0].msg = "Value error, values[1]: string 'oops' is not NaN/Infinity/-Infinity"
```

The offending raw value is not echoed back (redaction).

## 6. Correlation ids

Supplying `X-Request-Id: demo-trace-001` makes the same id appear in the
response header, the response body, and every structured log line emitted
while handling the request. Logs are single-line JSON and record only
redacted magnitude buckets of the input (e.g. `"+1e16"`, `"+1e0"`), never
the raw series.

## When the service refuses to decide

For inputs longer than `SUMAPI_REFERENCE_MAX_LENGTH` the oracle switches
from mpmath to 80-bit long double. If the resulting error interval
`[|e|-δ, |e|+δ]` straddles the verdict threshold, the method is reported
**inconclusive** with the interval, threshold and oracle precision, rather
than being silently accepted or rejected.
