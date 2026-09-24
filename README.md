# Point-Cloud Registration Service

Pure-backend offline **rigid point-cloud registration** service. It aligns two 3D
point clouds (up to **5000 points each**) with point-to-point **ICP**
(Iterative Closest Point) using SVD-based optimal rigid steps (Arun/Kabsch/Umeyama),
and serves the result over **HTTP/1.1** as JSON.

- Language/stdlib: **C++17**, POSIX sockets (thread-per-connection), no HTTP framework.
- Math: **Eigen 3.4.0**, vendored and checksum-pinned under `third_party/` (offline build).
- JSON: small self-contained parser/serializer (`src/json.h`).
- Crypto: self-contained **SHA-256** (`src/sha256.h`) with a FIPS 180-4 known-answer
  self-test at startup and real request/response integrity verification.
- The algorithm uses **only the two supplied clouds and an optional initial pose**.
  Ground truth from example generation is never read by the server.

## Build

Requires g++ ≥ 9 (C++17), CMake ≥ 3.16, and Python 3 (tests only). No network
access is needed at build time — Eigen is already vendored.

```bash
cmake -S . -B build -DCMAKE_BUILD_TYPE=RELEASE
cmake --build build -j$(nproc)
```

## Run

```bash
./build/pcr-server --host 0.0.0.0 --port 8080 --threads 4
# health check
curl -s http://127.0.0.1:8080/healthz
```

## Acceptance (one command)

```bash
./run_tests.sh
```

This builds everything, runs the C++ core tests, then boots the server on an
ephemeral port and runs the HTTP end-to-end suite.

## API

### `POST /v1/register`

Request (JSON):

| field | type | required | meaning |
|---|---|---|---|
| `source` | `[[x,y,z], ...]` | yes | cloud to move, ≤ 5000 finite points |
| `target` | `[[x,y,z], ...]` | yes | fixed reference cloud, ≤ 5000 points |
| `max_correspondence_distance` | number > 0 | no | pair rejection gate; default `0.1*scale` (adaptive), capped at `0.5*scale` |
| `max_iterations` | integer ≥ 1 | no | default 50, hard-capped at 200 |
| `initial_pose` | 4×4 matrix | no | initial rigid guess; its 3×3 block is projected onto SO(3) |

Each cloud may also be `{"points": [[x,y,z], ...]}`. Non-finite coordinates
(`NaN`/`Infinity`, accepted as the common JSON extension) are **dropped**, not
fitted, and counted in the response.

Optional integrity header `X-Content-SHA256: <hex>` — when present it must equal
the SHA-256 of the exact request body bytes or the server replies **422**.

Response (excerpt):

```json
{
  "ok": true,
  "converged": true,
  "high_confidence": true,
  "degenerate": false,
  "convergence_reason": "converged",
  "rotation": [[...3x3...]],
  "translation": [tx, ty, tz],
  "residual_rmse": 1.2e-14,
  "inlier_ratio": 1.0,
  "correspondences": 300,
  "iterations": 12,
  "rank_condition": 0.41,
  "scale": 2.0,
  "orthogonality_error": 0.0,
  "rotation_det": 1.0,
  "points": {"source_finite": 300, "target_finite": 300,
             "source_dropped_nonfinite": 0, "target_dropped_nonfinite": 0}
}
```

`transformed_source = rotation · source + translation`. The response carries an
`X-Content-SHA256` header over its body so clients can verify exact bytes.
`residual_rmse` is `null` when there are zero correspondences.

### `POST /v1/verify-hash`
Returns `{"sha256": "...", "length": N}` of the received body — demonstrates the
cryptographic operation is really executed server-side.

### `GET /healthz`
Liveness and service metadata.

## Convergence reasons (honest, no fake confidence)

- `converged` — scale-normalized RMSE change and rotation/translation increments
  are both below tolerance (an exact-data numerical floor is handled separately).
- `max_iterations` — iteration budget exhausted without stalling.
- `insufficient_correspondences` — fewer than 3 pairs.
- `degenerate_geometry` — the cross-covariance is rank-deficient (σmin/σmax below
  `rankTolerance` for several consecutive steps), e.g. collinear/planar clouds.
  The unobservable DOF is reported, **`high_confidence=false`**.
- `no_overlap` — zero pairs lie inside the correspondence gate.
- `invalid_input` — fewer than 3 finite points in a cloud, bad parameters, etc.

`high_confidence` requires all of: `converged`, not degenerate, ≥ 3
correspondences, `inlier_ratio ≥ 0.5`, and `rmse ≤ 0.05·scale`. Degenerate
fixtures therefore never return spurious high confidence.

## Correctness properties (asserted by the tests)

- Returned rotation satisfies `R·Rᵀ = I` and `det(R) = +1` to ≤ 1e-9 (the
  accumulated rotation is re-projected onto SO(3) via polar/SVD projection).
- **Known transform**: random cube under a known R, t is recovered with
  Frobenius rotation and translation errors `< 1e-6` and RMSE `< 1e-7`
  (deterministic seed; see reproducible bounds below).
- **Noise**: per-axis Gaussian σ = 0.002 → RMSE `< 5σ√3 ≈ 0.017`, pose error `< 1e-3`
  in both rotation (Frobenius) and translation. These follow the statistical scale
  `σ/(L√N)` with a several-σ safety margin and hold at a fixed seed.
- **Outliers**: 25% gross outliers + NaN/Inf rows are rejected/dropped; inlier
  ratio lands in `[0.70, 0.80]` and the pose is still correct.
- **Collinear** cloud → `degenerate_geometry`, never high confidence.
- **No-overlap** clouds (100 units apart) → `no_overlap`, zero correspondences.
- **Determinism**: identical requests produce byte-identical responses.

### Reproducible error bound

All randomness uses fixed seeds (C++ tests; `tools/gen_examples.py` seed
`20260923`). On the exact known-transform fixture the suite asserts:

```
||R_est − R_trueᵀ||_F < 1e-6,  |t_est + R_trueᵀ t_true| < 1e-6,  RMSE < 1e-7
```

Run `./run_tests.sh` — these numbers are checked, not just documented.

## Examples

Generate example request bodies (the script prints the ground truth it used;
that truth is **not** part of the request):

```bash
python3 tools/gen_examples.py
curl -s http://127.0.0.1:8080/v1/register \
  -H 'Content-Type: application/json' \
  --data-binary @examples/known_transform.json | python3 -m json.tool
curl -s http://127.0.0.1:8080/v1/register \
  -H 'Content-Type: application/json' \
  --data-binary @examples/collinear.json | python3 -m json.tool
```

## Locked dependency

| dependency | version | source | sha256 |
|---|---|---|---|
| Eigen | 3.4.0 | gitlab.com/libeigen/eigen/-/archive/3.4.0 | `8586084f71f9bde545ee7fa6d00288b264a2b7ac3607b974e54d13e7162c1c72` |

Recorded in `third_party/LOCK.txt`; the build includes only vendored headers.

## Layout

```
src/icp.{h,cpp}        ICP core: voxel NN, SVD rigid step, degeneracy detection
src/http_server.*      POSIX HTTP/1.1 server
src/api.*              JSON validation + endpoint handlers
src/sha256.h           SHA-256
src/json.h             JSON parser/serializer
src/main.cpp           server entry point
tests/test_icp.cpp     C++ acceptance tests (ctest)
tests/test_http.py     end-to-end HTTP tests
tools/gen_examples.py  example input generator
examples/              generated request bodies
third_party/           vendored Eigen + LOCK.txt
```
