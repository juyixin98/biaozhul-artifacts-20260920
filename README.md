# Battery Telemetry SOC Estimation Service

Offline state-of-charge (SOC) estimation for **synthetic** battery telemetry:
Coulomb counting with OCV-table calibration inside trusted rest windows.
Pure backend (FastAPI + NumPy), no frontend.

> **SAFETY**: This is an experimental, synthetic-model estimator.
> **It must NOT be used for real charging control** or any safety-related
> decision. Every API response carries a `not_for_control` banner.

## Model and conventions

| Item | Convention |
|---|---|
| Current sign | `current_a > 0` = **discharge**, `< 0` = **charge** (amperes) |
| Time | `t_s` seconds (epoch or relative), non-decreasing within a batch |
| Voltage | `voltage_v` terminal pack voltage (V); pack = `n_series_cells` x cell |
| Temperature | `temp_c` degrees Celsius |
| SOC | fraction in `[0, 1]`, always clamped; saturation is flagged |

**Coulomb counting** (trapezoidal):

```
dSOC = -(I_avg * eta) * dt / (3600 * Q_nom * k_T(T))
eta  = eta_discharge (I >= 0) | eta_charge (I < 0)
k_T  = temperature capacity factor, interpolated from the frozen table,
       CLAMPED outside the table (flag TEMP_OUT_OF_TABLE) - never extrapolated
```

**Data gaps** (`dt > max_interval_s`): a gap is *missing data*, not zero
current. No integration is performed across a gap, the following point is
flagged `GAP_BEFORE`, and the uncertainty budget grows by a bounded term.

**OCV calibration**: when `|I| <= rest_current_threshold_a` for at least
`min_rest_duration_s` **and** the temperature is inside the OCV-trusted
range, the terminal voltage is treated as OCV, mapped to SOC through the
frozen OCV table (linear interpolation, clamped), and the estimate snaps
to it. Calibration resets the bias-drift uncertainty term and records an
equivalent current-bias estimate as evidence. Outside the trusted
temperature range calibration is refused (`CALIBRATION_SKIPPED_UNTRUSTED`).

**Uncertainty** (`soc_sigma`, 1-sigma): measurement noise + random-walk
bias growth since the last calibration/anchor + bounded gap terms.

**Late data**: sessions accept late samples only inside
`[latest_t - replay_window_s, now)`; older late data is rejected with
reason `TOO_OLD_FOR_REPLAY` (never silently merged), duplicates with
`DUPLICATE_TIMESTAMP`. The first batch of a session is always accepted.

**Frozen parameters**: `params/params_v1.json` is signed with Ed25519
(`params/params_v1.sig`, public key `keys/params_signing_public.pem`).
The service verifies the signature at startup and **refuses to run** on
any mismatch. Parameter changes require a new version + new signature.

**Evidence**: every state-changing operation is appended to
`evidence/chain.jsonl` as a SHA-256 hash chain with per-record
HMAC-SHA256; `GET /api/v1/evidence/verify` recomputes both and fails
loudly on tampering. Sessions persist under `data/` and survive restarts.

## Layout

```
app/            service code (estimator, sessions, evidence, API)
params/         frozen parameter set + Ed25519 signature
keys/           dev-only signing keypair (private key is git-ignored)
tools/          sign_params.py (release tool), cli.py (offline CLI)
examples/       synthetic scenario inputs (JSON)
tests/          pytest suite (17 tests)
scripts/demo.sh end-to-end demo against a live server
```

## Quickstart

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt   # locked versions

# verify the frozen parameter signature (real Ed25519 check)
.venv/bin/python tools/cli.py --verify-params

# run the automated tests
.venv/bin/python -m pytest

# offline estimation (no server needed)
.venv/bin/python tools/cli.py --scenario mixed
.venv/bin/python tools/cli.py --input examples/sensor_dropout.json

# start the service
.venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8000
```

## Acceptance commands

```bash
# 1. tests (charge/discharge switch, bias accumulation, out-of-table
#    temperature, sensor outage, restart, replay window, crypto)
.venv/bin/python -m pytest -v

# 2. live server end-to-end demo (starts uvicorn, exercises the API,
#    verifies the evidence chain, then stops the server)
bash scripts/demo.sh

# 3. manual API checks (server running on :8000)
curl -s localhost:8000/health
curl -s localhost:8000/api/v1/params
curl -s -X POST localhost:8000/api/v1/estimate \
  -H 'Content-Type: application/json' \
  -d @examples/charge_discharge.json | python3 -m json.tool | head -40
curl -s localhost:8000/api/v1/evidence/verify
```

## API

| Method | Path | Purpose |
|---|---|---|
| GET | `/health` | liveness + safety banner |
| GET | `/api/v1/params` | frozen params version, digest, sign convention |
| POST | `/api/v1/estimate` | one-shot estimation over a sample batch |
| POST | `/api/v1/sessions` | create session + ingest first batch (201) |
| POST | `/api/v1/sessions/{id}/ingest` | ingest more (late data window enforced) |
| GET | `/api/v1/sessions/{id}` | current estimate + status |
| POST | `/api/v1/sessions/{id}/finalize` | freeze session, store final result |
| GET | `/api/v1/evidence/verify` | verify hash chain + HMACs |

Sample object: `{"t_s": float, "current_a": float, "voltage_v": float, "temp_c": float}`.

Point flags: `REST`, `CALIBRATED`, `CALIBRATION_SKIPPED_UNTRUSTED`,
`TEMP_OUT_OF_TABLE`, `GAP_BEFORE`, `SOC_SATURATED_HIGH`,
`SOC_SATURATED_LOW`, `SOC_UNINITIALIZED`.

## Re-signing parameters (release flow)

```bash
# edit params/params_v1.json (or create params_v2.json and update code),
# then re-sign with the release key:
python tools/sign_params.py --sign            # uses keys/params_signing_private.pem
python tools/sign_params.py                   # verify only
python tools/sign_params.py --generate-key    # create a fresh dev keypair
```

The committed public key/signature are DEVELOPMENT-ONLY; production
deployments must generate their own keypair and keep the private key
offline.
