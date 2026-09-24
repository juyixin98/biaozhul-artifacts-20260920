# Device Clock Drift Fitting Service (P076)

Pure-backend service that builds an **offline calibration of a device's
free-running counter against host time**: it estimates clock **offset and
linear drift** from multi-round request/response observations, filters loose
samples by round-trip time, tells **counter wraparound from device reboot**,
flags **time jumps and drift changes**, and — when evidence is too weak —
returns **`uncertain` instead of a guess**. Published calibrations are
**cryptographically signed, immutable, versioned, and carry a validity
window**; raw observations remember which calibration version was in force.

No symmetric-network-delay assumption is made anywhere: the result is always
an **asymmetric error interval**, not a single "true offset".

- Language/runtime: Python 3.12
- Stack: FastAPI + NumPy + SQLite (stdlib `sqlite3`) + HMAC-SHA256 (stdlib)
- No frontend; HTTP/JSON API only.

---

## 1. What it computes

For one continuous operating regime it fits

```
host_time = slope * device_counter_unwrapped + intercept
```

- `slope` is host-seconds per counter tick; the device's actual tick
  frequency is `1/slope`, reported as `drift_ppm` against `counter_nominal_hz`.
- Offset is reported at the counter-range midpoint as `offset_seconds` with an
  asymmetric `offset_interval [lower, upper]`.

### Bounds, not a point estimate

Each observed counter value has a **causality-only** feasible host-time
interval:

- a single counter stamp `c` observed during a round trip `[t0, t3]`
  satisfies `t0 ≤ host_time(c) ≤ t3`;
- when the device stamps both request arrival `c_recv` and response departure
  `c_send`, the directions are bounded independently (`host(c_recv) ≥ t0`,
  `host(c_send) ≤ t3`) — there is **no** `uplink = downlink` assumption.

The point estimate is a Huber-robust line on round-trip window midpoints; the
**error interval** combines 300-iteration grouped bootstrap with the
half-plane feasibility envelope (the set of lines consistent with every
causality bound). A strongly asymmetric path therefore *widens and shifts*
the interval instead of biasing a midpoint.

### Discontinuities

| event          | meaning                                                        | splits regime? |
|----------------|----------------------------------------------------------------|----------------|
| `wrap`         | modular counter rolled over; the device clock stayed continuous | no            |
| `reboot`       | counter reset near zero; clock state unknown                   | yes           |
| `time_jump`    | anomalous forward leap / backward step that is not a wrap      | yes           |
| `drift_change` | the counter-vs-host slope provably changes (F-test + slope gap)| yes           |
| `uncertain`    | a discontinuity is visible but cannot be classified            | yes (untrusted)|

Wraps are unwrapped **kinematically**: the service searches for an integer
number of modulus periods that makes the unwrapped advance agree with the
estimated counter rate, which also handles a gap that spans **multiple**
periods (e.g. a fast 16-bit counter sampled slower than its period). When
that cannot be established, it reports `uncertain` rather than guessing.

### When it refuses to publish

A regime is `calibrated` only when all of these hold; otherwise it is
`uncertain` with a machine-readable `reason`, and `?publish=true` will not
store a model:

- ≥ 6 samples survive RTT filtering (`MIN_POINTS_CALIBRATED`),
- the host-time span exceeds `2 ×` median RTT (`TIME_SPAN_VS_RTT`),
- the bootstrap yields finite slope confidence bounds.

A slope break is reported as `drift_change` only when it is statistically
separated from network jitter (stringent F threshold **and** a slope gap
exceeding 8 pooled standard errors). A real break buried under RTT noise on a
short span is deliberately **not** reported — that is the "insufficient
evidence" requirement, exercised by the tests.

---

## 2. Layout

```
app/
  main.py        FastAPI app, routes, dependency wiring
  schemas.py     request/response models
  fitting.py     RTT filter, interval-robust line fit, bootstrap,
                 wrap/reboot/jump/drift segmentation
  service.py     calibration orchestration, publishing, conversion
  storage.py     SQLite: devices, observations, immutable models, audit log
  crypto.py      HMAC-SHA256 over canonical JSON; 0600 key file
  lab.py         SIMULATED device endpoint (/lab/*) for acceptance only
scripts/
  probe.py       performs real HTTP request/response rounds -> samples
  demo.py        end-to-end acceptance scenario runner
examples/        ingest_*.json sample inputs
tests/           unit, API and real-network end-to-end tests
requirements.txt full hash-pinned lockfile (--require-hashes)
```

`/lab/*` is a clearly-labelled **emulator** used to inject asymmetric delays,
wraps, reboots and drift changes. The probe still makes genuine HTTP calls and
records `t0`/`t3` with `time.time()`; the uplink delay is really slept before
the POST and the downlink delay is really slept on the server after `c_send`
is stamped, so both directions genuinely land inside the measured RTT.

---

## 3. Local start

```bash
# from the project root
python3 -m venv .venv
. .venv/bin/activate
pip install --require-hashes -r requirements.txt

uvicorn app.main:app --host 127.0.0.1 --port 8000
```

Health check:

```bash
curl -s http://127.0.0.1:8000/health
# {"status":"ok","version":"1.0.0","devices":0,"models":0}
```

Environment variables:

| variable              | default                  | purpose                          |
|-----------------------|--------------------------|----------------------------------|
| `CLOCKDRIFT_DB`       | `./data/clockdrift.db`   | SQLite path                      |
| `CLOCKDRIFT_KEY_FILE` | `./data/signing.key`     | HMAC key (auto-created, mode 600)|

Interactive API docs are served by FastAPI at `/docs` (Swagger UI) — useful
for exploring request/response shapes even though there is no shipped
frontend.

---

## 4. API

### Ingest samples (and optionally publish)

`POST /api/v1/devices/{device_id}/samples?publish=false`

```json
{
  "device_id": "sensor-1",
  "counter_modulus": 65536,            // null/omit for an unbounded counter
  "counter_nominal_hz": 1000000,       // needed for offset seconds and ppm
  "samples": [
    {
      "t0": 1000.0,                   // host send time (seconds)
      "t3": 1000.02,                  // host receive time (>= t0)
      "device_counter": 61200.0,      // counter at request arrival
      "device_counter_send": 61250.0, // optional: counter at response departure
      "seq": 0
    }
  ]
}
```

Response: overall `status` (`calibrated`/`uncertain`), one report per
`segments`, `discontinuities` with human-readable `evidence`, and — when
published — `version`, `valid_from_host_time`, and the hex HMAC `signature`.

### Re-fit from stored history

`POST /api/v1/devices/{device_id}/calibrate?publish=true`

Observations are append-only and always store the calibration `version_used`
that was active when they arrived, so historical data can be re-interpreted
with the exact calibration in force at the time.

### Convert a counter reading to host time

`POST /api/v1/convert`

```json
{
  "device_id": "sensor-1",
  "device_counter": 12345.0,
  "host_time_hint": 1005.1,   // disambiguates wrap count / calibration window
  "version": null             // optional: pin to a specific model version
}
```

Returns a point plus an asymmetric `host_time_interval`, the `version` used,
`in_validity_range` (false ⇒ extrapolation), and the model signature. A stored
model that fails HMAC verification yields `status: uncertain` and is never
used.

### Other

- `GET  /api/v1/models` and `GET /api/v1/devices/{id}/models` — immutable
  model inventory with `valid_from`/`valid_to`/`superseded`.
- `GET  /api/v1/models/{version}/verify` — recompute and check the HMAC.
- `POST /api/v1/devices/{id}/samples` with `?publish=true` closes the
  previous open model's `valid_to` at the new model's `valid_from`; exactly
  one model per device is open-ended at any time.

### Lab endpoints (test harness, not calibration API)

`POST /lab/devices`, `POST /lab/devices/{id}/tick`,
`POST /lab/devices/{id}/reboot`, `POST /lab/devices/{id}/drift?ppm=…`,
`DELETE /lab/devices/{id}`.

---

## 5. Example inputs

```bash
# strongly asymmetric one-way delays, 145 ppm drift
curl -s -X POST "http://127.0.0.1:8000/api/v1/devices/demo-sensor-01/samples?publish=true" \
  -H 'Content-Type: application/json' \
  -d @examples/ingest_asymmetric.json | python3 -m json.tool

# 16-bit microsecond counter rolling over
curl -s -X POST "http://127.0.0.1:8000/api/v1/devices/demo-wrap-16bit/samples" \
  -H 'Content-Type: application/json' -d @examples/ingest_wrap.json | python3 -m json.tool

# only three samples -> status must be "uncertain", nothing published
curl -s -X POST "http://127.0.0.1:8000/api/v1/devices/demo-few/samples?publish=true" \
  -H 'Content-Type: application/json' -d @examples/ingest_few_samples.json | python3 -m json.tool
```

---

## 6. Acceptance commands

```bash
# full automated suite (math + API + real-HTTP end to end)
. .venv/bin/activate
python -m pytest

# skip the real-network tests if a port cannot be opened
python -m pytest --skip-slow

# live end-to-end acceptance against a running server
uvicorn app.main:app --port 8000 &
sleep 3
PYTHONPATH=scripts python scripts/demo.py
```

`scripts/demo.py` walks seven scenarios and asserts on the outcomes:

1. **asymmetric delay** — interval is non-degenerate and truth-bracketed;
2. **counter wrap** — every rollover is labelled `wrap`, regime stays intact;
3. **reboot** — a `reboot` discontinuity splits the series into two regimes;
4. **drift change** — a noise-resolvable slope break is found as
   `drift_change` with two fitted regimes;
5. **few samples** — `uncertain`, no model published;
6. **versions & signatures** — list models and verify an HMAC;
7. **validity window** — republishing closes the old model's `valid_to`.

### What the non-asymmetric guarantee looks like

Scenario 1 prints an interval width of tens of milliseconds (the injected
one-way delay) even though the point is reported as a single timestamp. Unit
test `test_asymmetric_delay_bounds_are_asymmetric_and_valid` additionally
checks the *true* device time lies inside the bounds for every sample, and
`test_unresolvable_drift_break_is_not_guessed` pins the no-guessing rule.

---

## 7. Cryptography

- Every published model is serialized with deterministic canonical JSON
  (sorted keys, compact separators, `allow_nan=False`) and authenticated with
  **HMAC-SHA256** using a 256-bit key from `secrets.token_bytes`.
- The key file is created `O_EXCL` with mode `0600`; verification uses
  `hmac.compare_digest` (constant time).
- Tampering with a stored payload is detected by both `/verify` and the
  convert path (`tests/test_api.py::test_signature_tamper_detection`).

This is an integrity/authenticity mechanism for stored calibrations, not a
public-key PKI: the signing key lives on the calibration host.

---

## 8. Honest limitations

- The **anchor** for dual-stamped samples is the RTT-window centre; it is a
  point-estimation convenience only and never tightens the reported interval,
  which always comes from the one-sided causality bounds.
- When wraps happen **multiple times per sampling interval**, unwrapping needs
  the nominal tick frequency (`counter_nominal_hz`); without it, very fast
  counters cannot always be unwrapped and the service says so.
- A drift break smaller than the RTT/span-limited slope standard error is not
  reported; collect over a longer span or with a tighter link.
- The `/lab` device is a simulator. Against real hardware you supply the same
  `{t0, t3, device_counter[, device_counter_send]}` records captured by your
  own probe; the estimator itself is simulator-free.
