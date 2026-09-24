"""Device clock drift calibration service (pure backend)."""

from __future__ import annotations

import os
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Query
from pydantic import BaseModel, Field, field_validator

from .analyzer import analyze
from .config import AnalyzerConfig
from .crypto import Signer, verify_envelope
from .fitting import Observation
from .predict import convert_counter
from .storage import Store

DATA_DIR = Path(os.environ.get("CLOCKCAL_DATA_DIR", "data"))


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------


class SampleIn(BaseModel):
    t_send: float = Field(..., description="host time (s, e.g. unix) request left")
    t_recv: float = Field(..., description="host time (s) response arrived")
    counter: float = Field(..., description="device counter in the response")

    @field_validator("t_recv")
    @classmethod
    def _order(cls, v, info):
        ts = info.data.get("t_send")
        if ts is not None and v < ts:
            raise ValueError("t_recv must be >= t_send")
        return v


class AnalyzeRequest(BaseModel):
    device_id: str = Field(..., min_length=1, max_length=128)
    modulus: float | None = Field(
        None, gt=0, description="counter modulus; null if unknown"
    )
    samples: list[SampleIn] = Field(..., min_length=1)
    config_overrides: dict = Field(default_factory=dict)


class PublishRequest(BaseModel):
    device_id: str = Field(..., min_length=1, max_length=128)
    modulus: float | None = Field(None, gt=0)
    samples: list[SampleIn] = Field(..., min_length=1)
    valid_from: float | None = None
    valid_to: float | None = None
    force: bool = Field(False, description="publish even when status is uncertain")
    config_overrides: dict = Field(default_factory=dict)


class ConvertRequest(BaseModel):
    device_id: str
    counter: float
    at: float | None = Field(None, description="host time selecting the version")


# ---------------------------------------------------------------------------
# App / state
# ---------------------------------------------------------------------------


def create_app(data_dir: Path | None = None) -> FastAPI:
    data_dir = data_dir or DATA_DIR

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        # Defer key generation / DB creation until the server actually starts,
        # so merely importing this module (e.g. by tests) never writes to disk.
        app.state.signer = Signer(data_dir)
        app.state.store = Store(data_dir / "clockcal.sqlite", app.state.signer)
        app.state.now = time.time
        yield
        app.state.store.close()

    app = FastAPI(
        title="Device Clock Drift Calibration",
        version="1.0.0",
        lifespan=lifespan,
    )
    app.state.now = time.time

    def get_store() -> Store:
        return app.state.store

    def now() -> float:
        return app.state.now()

    def _run_analysis(req) -> object:
        overrides = {}
        for k, v in (req.config_overrides or {}).items():
            if not hasattr(AnalyzerConfig, k):
                raise HTTPException(400, f"unknown config key: {k}")
            overrides[k] = v
        cfg = AnalyzerConfig(**overrides)
        obs = [
            Observation(s.t_send, s.t_recv, s.counter) for s in req.samples
        ]
        return analyze(
            obs, device_id=req.device_id, modulus=req.modulus, config=cfg
        )

    # ------------------------------------------------------------------
    @app.get("/health")
    def health():
        return {"status": "ok", "signing_alg": "Ed25519",
                "public_key": app.state.signer.public_key_b64()}

    @app.post("/api/v1/calibrations/analyze")
    def analyze_endpoint(req: AnalyzeRequest):
        try:
            result = _run_analysis(req)
        except ValueError as exc:
            raise HTTPException(422, str(exc))
        return result.to_dict()

    @app.post("/api/v1/calibrations/publish")
    def publish_endpoint(req: PublishRequest, st: Store = Depends(get_store)):
        try:
            result = _run_analysis(req)
            envelope = st.publish_from_analysis(
                result,
                now=now(),
                valid_from=req.valid_from,
                valid_to=req.valid_to,
                force=req.force,
            )
        except ValueError as exc:
            raise HTTPException(409, str(exc))
        st.upsert_device(req.device_id, req.modulus, now())
        return {"analysis_status": result.status, "signed_version": envelope}

    @app.get("/api/v1/devices/{device_id}/versions")
    def list_versions(device_id: str, st: Store = Depends(get_store)):
        return {"device_id": device_id,
                "versions": st.list_versions(device_id)}

    @app.get("/api/v1/versions/{version_id}")
    def get_version(version_id: str, st: Store = Depends(get_store)):
        env = st.get_version(version_id)
        if env is None:
            raise HTTPException(404, "unknown version")
        return env

    @app.post("/api/v1/convert")
    def convert_endpoint(body: ConvertRequest, st: Store = Depends(get_store)):
        at = body.at if body.at is not None else now()
        env = st.active_version(body.device_id, at=at)
        if env is None:
            raise HTTPException(
                404,
                f"no published calibration version of "
                f"{body.device_id!r} valid at host time {at}",
            )
        out = convert_counter(env, body.counter)
        out["queried_at"] = at
        st.record_conversion(
            {
                "device_id": out["device_id"],
                "version_id": out["version_id"],
                "epoch": out["epoch"],
                "counter": out["counter"],
                "host_point": out["host_time"]["point"],
                "host_lower": out["host_time"]["lower"],
                "host_upper": out["host_time"]["upper"],
                "warning": out["warning"],
            },
            now=now(),
        )
        return out

    @app.get("/api/v1/devices/{device_id}/history")
    def history(
        device_id: str,
        limit: int = Query(100, ge=1, le=1000),
        st: Store = Depends(get_store),
    ):
        return {"device_id": device_id,
                "conversions": st.list_conversions(device_id, limit)}

    @app.post("/api/v1/verify")
    def verify_endpoint(envelope: dict):
        ok, msg = verify_envelope(envelope)
        return {"valid": ok, "message": msg}

    return app


app = create_app()
