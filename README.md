# Strided Tensor Backend

A small, self-contained backend for **continuous and non-contiguous tensors**:
slicing, transposition, reshape and basic elementwise/matrix operations over
explicit `(shape, strides, storage offset)` layouts, with a computation graph,
versioned training state and an independent NumPy-oracle validation suite.

Stack: **Python 3.12 · FastAPI · NumPy · pytest**. All data is local and
synthetic — there are no external accounts or real business datasets.

---

## 1. What is implemented

### Tensor type — `tensor_backend/tensor/`
- A tensor is a view `(shape, strides, offset)` over one contiguous
  **float64** storage buffer (`storage.py`). Storage always copies its input,
  so owned buffers never alias caller arrays.
- **Layout math** (`layout.py`, pure functions, no NumPy storage):
  - shape/stride/offset normalization with C/F stride construction;
  - **out-of-bounds detection before access** — every view is checked against
    the buffer using the reachable index interval, including negative
    strides (`address_bounds` / `validate_bounds`);
  - **size-multiplication overflow detection** before allocation
    (`ShapeOverflowError`, addressable cap `2**63 - 1`, plus a configurable
    element limit);
  - basic indexing with arbitrary per-axis slices, including **negative
    steps** and integer-index axis collapse;
  - transpose via arbitrary axis permutations;
  - broadcasting that assigns literal **zero strides** to stretched/new axes;
  - **zero-copy reshape feasibility** (see below).
- **Reshape** (`zero_copy_reshape`): returns view strides only when a view
  exists, otherwise raises `ReshapeCopyRequiredError` with the specific reason.
  `Tensor.reshape(..., allow_copy=True)` performs the explicit copy.
- **Overlapping writes** (`ops.py`):
  - writing into a **self-overlapping** destination (distinct elements mapped
    to one storage position, e.g. a zero stride on a size>1 axis) is always
    rejected — the value itself is undefined, no temporary can invent it;
  - **source/destination overlap** is rejected under `overlap_policy="raise"`
    and executed deterministically under `"temp"` (the right-hand side is fully
    materialized into a contiguous temporary before writing);
  - an identical 1:1 layout (`x += x`-style in-place update) is allowed.

### Computation graph — `tensor_backend/graph/`
A real DAG, not a scripted demo: handles name tensors, each operation records a
node with opcode, input tensor ids, output layout, and whether the operation
**aliased storage or copied**. Plans are JSON (`execute_plan`) and the graph
exposes a full trace plus an aliasing report.

### Training state — `tensor_backend/training/`
A genuine linear model `y = X @ W + b` trained by SGD on analytic MSE gradients
computed through the strided ops (the bias is added through a zero-stride
broadcast). Every update bumps a monotonic **version**, records its
**location** and the originating **request id**, and keeps a loss history.

### Numerical validation — `tensor_backend/validation/`
A fixed suite plus a large randomized differential test. **Reference answers
are generated directly by NumPy, never by the core under test.** Each check
asserts concrete values *and* the copy/alias category, and reports
`pass / fail / uncertain / error` with a distinct `failure_category`.

### API — `tensor_backend/api/`
FastAPI service with request-correlation middleware (`x-request-id` echoed in
the response header and body), JSON structured logs (request id, version,
location, failure category, uncertainties listed separately), and explicit
error categories from `tensor_backend/tensor/errors.py`.

| Method & path | Purpose |
|---|---|
| `GET /health` | service/version/bounds |
| `GET /ops` | supported opcodes and overlap policies |
| `POST /graphs` | create constants + run a plan; returns values, trace, aliasing |
| `GET /graphs/{id}/tensors/{handle}` | fetch one tensor from a stored graph |
| `POST /reshape/check` | zero-copy feasibility for an arbitrary layout |
| `POST /validate` | run the NumPy-oracle suite |
| `POST /train/linear` | run SGD and return versioned losses/parameters |

---

## 2. Key contracts and trade-offs

1. **Reshape zero-copy criterion.** Let `f(k)` be the storage index of the
   `k`-th element in C-order traversal of the source. A view reshape exists
   exactly when the target strides forced by `new_stride[i] = f(B_i) − f(0)`
   (`B_i = prod(target[i+1:])`) reproduce `f(k)` at every element. The
   implementation constructs those forced strides and proves equality at `k=0`
   and every row boundary of both layouts (the maps are piecewise affine, so
   boundary probes prove equality on the entire range). This was differential-
   tested against NumPy on **tens of thousands of random layouts with zero
   mismatches**.
2. **Empty tensors.** A zero-element view accesses no storage, so it can never
   be out of bounds and every same-count reshape is served as a view. This is
   deliberately *more permissive* than NumPy (which sometimes copies empty
   arrays); it is sound because no element is ever read or written.
3. **Negative strides** are first-class for slicing/views (reverse, step `-2`,
   multi-axis) and a reversed 1-D array reshape remains a view, matching NumPy.
4. **Overlap semantics distinguish two cases** (self-overlap vs. source/dest
   overlap) rather than silently copying; the caller chooses `raise` or `temp`.
5. **dtype:** float64 only, intentionally (one fixed itemsize keeps the
   element/byte stride contract explicit).
6. Exact self-overlap detection enumerates storage offsets up to 200 000
   elements; larger tensors use a structural check and surface an explicit
   **uncertain** result instead of a false negative.

### Explicit failure categories
`shape_overflow`, `out_of_bounds`, `invalid_layout`, `invalid_stride`,
`axis_error`, `broadcast_error`, `reshape_copy_required`,
`overlapping_write`, `dtype_error`.

---

## 3. Local startup

```bash
# from the repository root
python3 -m pip install -r requirements.txt   # versions pinned
python3 -m uvicorn tensor_backend.api.app:app --host 127.0.0.1 --port 8000
```

Optional environment variables: `TENSOR_BACKEND_HOST`, `TENSOR_BACKEND_PORT`,
`TENSOR_BACKEND_MAX_NDIM` (default 32), `TENSOR_BACKEND_MAX_ELEMENTS`
(default 100 000 000), `TENSOR_BACKEND_LOG_UNCERTAINTIES`.

No server needed for the command line:

```bash
python3 -m tensor_backend.cli validate   # NumPy-oracle suite
python3 -m tensor_backend.cli demo       # build a graph + train, print trace
```

---

## 4. Example requests

```bash
# transpose (view) then reshape (copy), plus a zero-copy flatten
curl -s -X POST localhost:8000/graphs \
  -H 'Content-Type: application/json' \
  -H 'x-request-id: req-example-1' \
  --data @examples/graph_transpose_reshape.json

# broadcast zero stride + elementwise ops
curl -s -X POST localhost:8000/graphs \
  -H 'Content-Type: application/json' \
  --data @examples/graph_broadcast.json

# overlapping slices: deterministic result through a temporary copy
curl -s -X POST localhost:8000/graphs \
  -H 'Content-Type: application/json' \
  --data @examples/graph_overlap_temp.json

# ask whether an arbitrary layout can reshape without copying
curl -s -X POST localhost:8000/reshape/check \
  -H 'Content-Type: application/json' \
  -d '{"shape":[4,3],"strides":[1,4],"new_shape":[12]}'

# run the independent validation suite
curl -s -X POST localhost:8000/validate | python3 -m json.tool
```

Python usage:

```python
from tensor_backend.tensor import Tensor, ops
import numpy as np

t = Tensor.from_values(np.arange(12).reshape(3, 4))
tr = t.T                         # view, strides (1,4), shares storage
tr.reshape((12,))                # raises ReshapeCopyRequiredError
flat = tr.reshape((12,), allow_copy=True)   # explicit copy, new storage
t.reshape((12,))                 # zero-copy view of the contiguous source

v = Tensor.from_layout(np.arange(3.0), shape=(3, 3), strides=(0, 1))
v.self_overlap()                 # (True, False): 9 elements -> 3 positions
```

---

## 5. Tests

```bash
python3 -m pytest                 # 68 tests
```

- `tests/test_layout.py` — unit tests for stride math, bounds/overflow, slices,
  reshape decisions, broadcasting.
- `tests/test_differential.py` — independent NumPy differential tests
  (exhaustive random reshape view/copy verdicts; random view reconstruction;
  broadcasting; negative strides).
- `tests/test_tensor_ops.py` — views, ops, overlap policy, empty tensors,
  error categories.
- `tests/test_graph.py` — plan execution, trace, aliasing report.
- `tests/test_training.py` — real SGD convergence, versioning, request ids.
- `tests/test_api.py` — end-to-end HTTP tests including failure categories.

Tests assert concrete results and specific failure categories — not merely that
an interface is callable — and the oracles are NumPy, independent of the core.

## 6. Module layout

```
tensor_backend/
  config.py                 # env configuration, bounds
  tensor/  errors.py layout.py storage.py tensor.py ops.py
  graph/   graph.py
  training/state.py
  validation/validator.py
  api/     app.py logging_setup.py
  cli.py
tests/                       # pytest suite (independent NumPy oracles)
examples/                    # JSON request bodies
requirements.txt             # pinned dependency lock
```
