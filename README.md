# Asynchronous EKF Fusion Service

Pure-backend service that fuses 2-D **odometry (velocity)** and **GNSS
(position)** measurements arriving on a 2-D constant-velocity EKF.  Messages
carry their own measurement timestamp and are fused **strictly in
measurement-time order**, never in arrival order.  Late messages inside a
2 s window trigger a deterministic replay from a cached checkpoint; older
messages are refused without touching the state.

* Language/runtime: Python 3.10+ (developed on 3.12), NumPy, FastAPI, uvicorn
* State: `x = [px, py, vx, vy]`; continuous white-noise-acceleration process
  model; GNSS observes position, odometry observes velocity.
* Stable covariance: symmetrisation + eigenvalue projection to the PSD cone
  after every predict/update; measurement updates use the **Joseph form**.
* Outliers are rejected by a **NIS / chi-squared gate** (default threshold
  `9.2103`, χ² with 2 dof at p=0.99), and every rejection keeps evidence
  (innovation, NIS, threshold, predicted state).
* Integrity: a **SHA-256 hash chain** over every accepted step, exposed at
  `/integrity`; optional per-session **HMAC-SHA256** request authentication
  (real `hmac`/`secrets` operations, constant-time comparison).
* Bounded checkpoint cache: snapshots older than the replay window are
  pruned while one baseline checkpoint at the oldest surviving timeline
  point is retained, so replay remains possible and memory stays bounded.

## Layout

```
ekf_fusion/
  ekf.py        # EKF math: F, Q, H, Joseph-form update, PSD enforcement
  engine.py     # time ordering, checkpoint/replay, gating, SHA-256 chain
  crypto.py     # HMAC-SHA256 auth + CSPRNG session keys/ids
  app.py        # FastAPI routes and request validation
examples/
  generate_example_inputs.py   # builds measurements.json (deterministic)
  measurements.json            # 151 bounded-late shuffled messages
  demo_client.py               # one-command end-to-end acceptance demo
tests/
  test_ekf.py      # filter mathematics, covariance guards, gate
  test_engine.py   # ordering/replay identity, long gap, rollback, tampering
  test_api.py      # HTTP routes, validation, batch, HMAC auth
requirements.txt      # direct, version-bounded dependencies
requirements.lock     # exact pinned versions (pip freeze)
```

## Local startup

```bash
cd /path/to/project
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.lock   # or: pip install -r requirements.txt

uvicorn ekf_fusion.app:app --host 127.0.0.1 --port 8000
```

Interactive API docs: http://127.0.0.1:8000/docs

## Acceptance commands

```bash
# 1) automated tests (29 tests)
python -m pytest -q

# 2) end-to-end demo: starts its own uvicorn on :8765, feeds the shuffled
#    data set, verifies shuffled==ordered traces, hash chain, gating,
#    late/duplicate rejection, and ground-truth accuracy; prints RESULT: PASS
python examples/generate_example_inputs.py   # regenerate inputs (optional)
python examples/demo_client.py
```

## Protocol

### Sessions

`POST /sessions` — body (all optional):

```json
{"q": 1.0, "gate": 9.210340371976184, "late_window_s": 2.0,
 "require_hmac": false}
```

Response includes a random `session_id` and `hmac_key` (32 bytes hex).
Sessions are independent, in-memory, and serialised by an `asyncio.Lock` per
session, so concurrent requests cannot interleave a replay.

### Measurement

`POST /sessions/{id}/measurements`

```json
{
  "message_id": "g0001",
  "t": 1.234,
  "kind": "gnss",
  "z": [1.23, 0.50],
  "R": [[0.0225, 0.0], [0.0, 0.0225]]
}
```

`kind` is `"gnss"` (`z = [x, y]`) or `"odom"` (`z = [vx, vy]`).  `R` must be
a finite, symmetric, strictly positive-definite 2×2 matrix; otherwise the
message is rejected per-item with a machine-readable reason
(`covariance_shape|covariance_non_finite|covariance_not_symmetric|
covariance_not_psd|covariance_not_positive_definite`) and never enters the
timeline.

Ingestion outcomes (HTTP 200 body):

| reason                  | meaning                                                    |
|-------------------------|------------------------------------------------------------|
| accepted                | fused; `step` contains state + innovation diagnostics      |
| duplicate_message_id    | id already seen                                            |
| late_too_old            | `t < high_water - late_window_s` (2 s), state untouched    |
| outlier_gate            | NIS > gate; evidence stored, state left at prediction      |
| time_non_finite         | non-finite timestamp                                       |

Accepted responses include `"replayed": true` when the arrival was late and
the result was produced by restoring a checkpoint and re-running.

`POST /sessions/{id}/measurements/batch` accepts `{"measurements":[...]}` and
processes each item independently under one lock (one bad item never aborts
the others), returning per-item results plus the current state.

### Read-outs

* `GET  /sessions/{id}/state` — current state `[px,py,vx,vy]`, covariance,
  PSD flag, min eigenvalue, counts, hash head, high-water mark.
* `GET  /sessions/{id}/steps?limit=N` — every accepted step in
  measurement-time order: predicted/updated `x`/`P`, innovation `ν`,
  innovation covariance `S`, NIS, Kalman gain, PSD-clip diagnostics and the
  per-step `chain_hash`.
* `GET  /sessions/{id}/rejections` — evidence for gate rejections.
* `GET  /sessions/{id}/integrity` — recomputes the SHA-256 hash chain over
  the ordered steps and reports `ok`, stored vs recomputed head, and any
  mismatched steps.
* `DELETE /sessions/{id}`, `GET /health`.

### HMAC authentication

Create the session with `"require_hmac": true`.  Every measurement must then
carry `signature`: hex HMAC-SHA256 (key = session key) over the canonical
message string (one field per line):

```
v1
<message_id>
<repr(float t)>
<kind>
<repr(z0)>,repr(z1)
<repr(R00)>,repr(R01)>,repr(R10)>,repr(R11)
```

Python example:

```python
from ekf_fusion.crypto import canonical_message, hmac_sign
payload = canonical_message("g1", 1.0, "gnss", [1.0, 0.4],
                            [[0.0225, 0.0], [0.0, 0.0225]])
sig = hmac_sign(key, payload)
```

Missing/invalid signatures return HTTP 401 (batch items are marked
`invalid_signature`).  See `tests/test_api.py::test_hmac_enforced_session_*`.

## Semantics guaranteed by the tests

1. **Order invariance.** The final state, every per-step state/innovation and
   the hash chain are bit-for-bit identical (`atol=1e-12`) between a session
   fed a bounded-late shuffled stream and one fed the time-sorted stream.
2. **Long blackout.** A 30 s measurement gap propagates without failure,
   covariance grows as required, stays symmetric PSD, and the estimate
   recovers to within 1 m / 0.2 m/s once dense measurements resume.
3. **Bad covariance.** Asymmetric / indefinite / non-finite / wrong-shaped
   `R` is rejected before ingestion with explicit reasons; the timeline is
   unchanged.
4. **Time rollback.** A message older than `high_water - 2 s` is refused
   (`late_too_old`) without state mutation; duplicate ids are refused; an
   in-window late message is inserted and replayed from a checkpoint.
5. **Outliers.** Gross blips exceed the NIS gate, do not move the state, and
   leave full evidence (`GET /rejections`); they remain rejected after later
   replays.
6. **Tamper evidence.** Modifying any stored step breaks the recomputed hash
   chain at exactly that step.

## Quick manual check with curl

```bash
curl -s localhost:8000/health
SID=$(curl -s -X POST localhost:8000/sessions -H 'content-type: application/json' \
  -d '{}' | python -c 'import sys,json;print(json.load(sys.stdin)["session_id"])')
curl -s -X POST localhost:8000/sessions/$SID/measurements \
  -H 'content-type: application/json' \
  -d '{"message_id":"g1","t":0.0,"kind":"gnss","z":[0,0],
       "R":[[0.04,0],[0,0.04]]}'
curl -s localhost:8000/sessions/$SID/integrity
```
