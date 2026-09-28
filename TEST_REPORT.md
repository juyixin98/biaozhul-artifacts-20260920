# Test report

Run date: 2026-09-28 · Python 3.12.3 · NumPy 2.4.6 · FastAPI 0.141.1 · pytest 9.1.1

## Final result

```
python3 -m pytest
======================== 86 passed, 1 warning in 1.7s ========================
```

Coverage (`python3 -m coverage run --source=tensor_backend -m pytest`):

| Module | Cover |
|---|---|
| tensor/layout.py | 98% |
| tensor/ops.py | 90% |
| tensor/tensor.py | 93% |
| tensor/storage.py | 82% |
| tensor/errors.py | 100% |
| graph/graph.py | 85% |
| training/state.py | 95% |
| validation/validator.py | 81% |
| api/app.py | 88% |
| api/logging_setup.py | 93% |
| config.py | 85% |
| cli.py | 97% |
| **total** | **90%** (every source file ≥ 80%) |

The NumPy-oracle suite (`POST /validate` / `python3 -m tensor_backend.cli validate`):

```
{'total': 8, 'passed': 8, 'failed': 0, 'uncertain': 0, 'errors': 0}
```

The randomized differential reshape test compares the core's view/copy verdict
and concrete values against NumPy for **>15,000 random layouts × every target
factorization** — zero mismatches. A standalone 40,000-case differential probe
was also run during development with zero mismatches.

## Real failures found during development (caught by the oracle/tests, then fixed)

These were genuine implementation defects, not test typos — the independent
NumPy oracle and the fixed suite surfaced them:

1. **Empty-view out-of-bounds false positive.** `validate_bounds` rejected an
   empty view `(0,3)` even over a zero-length buffer because it checked the
   offset point against an empty buffer. An empty view accesses no position, so
   it cannot be out of bounds. Fixed with an explicit empty-view early return.
   Caught by `check_empty_tensor` (`error: out_of_bounds`).
2. **Storage aliased caller arrays.** `Storage.allocate` used
   `np.ascontiguousarray`, which does not copy an already-contiguous input. Two
   "independent" fixtures ended up sharing one NumPy buffer, which corrupted the
   NumPy oracle mid-comparison. Fixed to force a copy. Caught by
   `check_overlapping_slices` (`fail: value_mismatch`).
3. **Empty shape erased through a list round-trip.** An empty tensor's
   `to_list()` is `[]`; rebuilding `np.asarray([])` dropped the `(0,4)` shape,
   so a correct empty broadcast was reported as a value mismatch. Validation now
   compares `materialize()` arrays directly.
4. **0-d scalar promoted to shape `(1,)` by ops.** `np.ascontiguousarray` on a
   0-d result returns shape `(1,)`, so `square(scalar)` changed rank. Replaced
   with a shape-preserving contiguous copy. Locked in by
   `test_scalar_0d_keeps_rank_through_ops`.
5. **Boolean index silently accepted as integer.** `True`/`False` were treated
   as ints before the boolean guard ran; now rejected with `invalid_layout`.

## Deliberate behavioral differences from NumPy (documented, not failures)

- **Empty-array reshape is always a zero-copy view** here, while NumPy sometimes
  copies `(0,…)` reshapes. Sound because no element is ever read/written.
- Exact self-overlap enumeration is capped at 200,000 elements; beyond that the
  backend reports an explicit `uncertain` result rather than a false negative.
  No current test exercises that path, so there are **0 uncertain** results.

## Not executed / out of scope

- No remote/cloud deployment, database, or authentication path exists; nothing
  in those categories was run or is applicable.
- Non-float64 dtypes are intentionally unsupported (`dtype_error` on int32
  storage); no integer/tensor-type test matrix exists by design.
- The HTTP server was exercised live on localhost (see README examples) and via
  FastAPI's in-process `TestClient`; no external network egress is used.
