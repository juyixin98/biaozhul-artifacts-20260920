"""FastAPI application: device clock drift fitting service."""

from __future__ import annotations

import os
import uuid
from typing import Optional

from fastapi import Depends, FastAPI, HTTPException, Query
from pydantic import BaseModel

from . import __version__, crypto
from .fitting import Observation
from .lab import router as lab_router
from .schemas import (
    CalibrationResponse,
    ConvertRequest,
    ConvertResponse,
    HealthResponse,
    IngestRequest,
)
from .service import Service
from .storage import Store

app = FastAPI(
    title="Device Clock Drift Fitting Service",
    version=__version__,
    description=(
        "Offline calibration of device free-running counters against host "
        "time: offset + linear drift, RTT-based outlier filtering, wrap vs "
        "reboot identification, signed versioned models with validity windows."
    ),
)

_store: Optional[Store] = None
_service: Optional[Service] = None


def get_store() -> Store:
    global _store
    if _store is None:
        _store = Store(os.environ.get("CLOCKDRIFT_DB", "./data/clockdrift.db"))
    return _store


def get_service() -> Service:
    global _service
    if _service is None:
        _service = Service(get_store(), crypto.load_or_create_key())
    return _service


def _to_observations(req: IngestRequest) -> list[Observation]:
    return [
        Observation(
            t0=s.t0, t3=s.t3, c_recv=s.device_counter,
            c_send=s.device_counter_send, seq=s.seq,
        )
        for s in req.samples
    ]


@app.post("/api/v1/devices/{device_id}/samples", response_model=CalibrationResponse)
def ingest(device_id: str, req: IngestRequest,
           publish: bool = Query(False, description="publish a signed model if fit succeeds"),
           svc: Service = Depends(get_service)) -> CalibrationResponse:
    if req.device_id != device_id:
        raise HTTPException(400, "device_id in path and body must match")
    ingest_id = uuid.uuid4().hex
    return svc.run_calibration(
        device_id=device_id,
        modulus=req.counter_modulus,
        nominal_hz=req.counter_nominal_hz,
        observations=_to_observations(req),
        publish=publish,
        ingest_id=ingest_id,
    )


class CalibrateRequest(BaseModel):
    counter_modulus: Optional[float] = None
    counter_nominal_hz: Optional[float] = None


@app.post("/api/v1/devices/{device_id}/calibrate",
          response_model=CalibrationResponse)
def calibrate_from_history(
    device_id: str,
    body: CalibrateRequest | None = None,
    publish: bool = Query(False),
    svc: Service = Depends(get_service),
    store: Store = Depends(get_store),
) -> CalibrationResponse:
    """Re-fit (and optionally publish) from all previously ingested samples."""
    rows = store.get_observations(device_id)
    if not rows:
        raise HTTPException(404, f"no samples ingested for device {device_id!r}")
    dev = store.get_device(device_id)
    modulus = (body.counter_modulus if body and body.counter_modulus is not None
               else (dev["counter_modulus"] if dev else None))
    hz = (body.counter_nominal_hz if body and body.counter_nominal_hz is not None
          else (dev["counter_nominal_hz"] if dev else None))
    obs = [
        Observation(r["t0"], r["t3"], r["c_recv"], r["c_send"], r["seq"])
        for r in rows
    ]
    return svc.run_calibration(
        device_id=device_id, modulus=modulus, nominal_hz=hz,
        observations=obs, publish=publish,
        ingest_id=f"recal-{uuid.uuid4().hex}",
    )


@app.post("/api/v1/convert", response_model=ConvertResponse)
def convert(req: ConvertRequest, svc: Service = Depends(get_service)) -> ConvertResponse:
    return svc.convert(
        device_id=req.device_id,
        raw_counter=req.device_counter,
        hint=req.host_time_hint,
        version=req.version,
    )


@app.get("/api/v1/devices/{device_id}/models")
def list_device_models(device_id: str, svc: Service = Depends(get_service)):
    return [m.model_dump() for m in svc.list_models(device_id)]


@app.get("/api/v1/models")
def list_all_models(svc: Service = Depends(get_service)):
    return [m.model_dump() for m in svc.list_models(None)]


class VerifyResponse(BaseModel):
    version: str
    valid: bool
    detail: str


@app.get("/api/v1/models/{version}/verify", response_model=VerifyResponse)
def verify_model(version: str,
                 svc: Service = Depends(get_service),
                 store: Store = Depends(get_store)) -> VerifyResponse:
    import json

    row = store.get_model(version)
    if row is None:
        raise HTTPException(404, "unknown model version")
    payload = json.loads(row["payload_json"])
    ok = crypto.verify(payload, row["signature"], svc.key)
    return VerifyResponse(
        version=version, valid=ok,
        detail="HMAC-SHA256 over canonical JSON verified" if ok
        else "signature mismatch — stored payload has been altered",
    )


@app.get("/health", response_model=HealthResponse)
def health(store: Store = Depends(get_store)) -> HealthResponse:
    return HealthResponse(
        status="ok", version=__version__,
        devices=store.count_devices(), models=store.count_models(),
    )


app.include_router(lab_router)
