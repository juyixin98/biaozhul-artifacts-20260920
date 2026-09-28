"""FastAPI control plane over a :class:`Coordinator`.

Thin HTTP shell: every endpoint delegates to the coordinator and returns
its structured result.  Rejected submissions are *data*, not HTTP errors —
the body carries ``accepted: false`` plus the machine-readable ``reason``
and the diagnostic ``record_id`` so a caller can trace exactly why.

Arrays cross the wire as JSON lists (teaching runtime, small tensors).
"""

from __future__ import annotations

import uuid
from typing import Dict, List

import numpy as np
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from bucket_sync.coordinator import Coordinator


class BeginRoundBody(BaseModel):
    participants: List[str] = Field(min_length=1)
    skipped: List[str] = Field(default_factory=list)


class SubmitBucketBody(BaseModel):
    round_id: int
    worker_id: str
    bucket_index: int
    segment: List[float]
    present_mask: List[bool]
    n_samples: int
    base_token: str


class LostBody(BaseModel):
    reason: str = "reported_dead"


class SkipBody(BaseModel):
    pass


def _params_to_json(params: Dict[str, np.ndarray]) -> Dict[str, list]:
    return {k: np.asarray(v, dtype=np.float64).tolist() for k, v in params.items()}


def create_app(coordinator: Coordinator) -> FastAPI:
    app = FastAPI(title="bucket-sync teaching runtime")

    @app.middleware("http")
    async def request_id_middleware(request: Request, call_next):
        request_id = request.headers.get("x-request-id") or f"req-{uuid.uuid4().hex[:12]}"
        request.state.request_id = request_id
        response = await call_next(request)
        response.headers["x-request-id"] = request_id
        return response

    @app.exception_handler(KeyError)
    async def key_error_handler(request: Request, exc: KeyError):
        return JSONResponse(
            status_code=404,
            content={"error": "not_found", "detail": str(exc.args[0] if exc.args else exc)},
        )

    @app.exception_handler(ValueError)
    async def value_error_handler(request: Request, exc: ValueError):
        return JSONResponse(
            status_code=422, content={"error": "invalid", "detail": str(exc)}
        )

    @app.exception_handler(RuntimeError)
    async def runtime_error_handler(request: Request, exc: RuntimeError):
        return JSONResponse(
            status_code=409, content={"error": "conflict", "detail": str(exc)}
        )

    @app.get("/health")
    def health() -> dict:
        return {"status": "ok", "generation": coordinator.current_generation()}

    @app.post("/workers/{worker_id}/register")
    def register(worker_id: str) -> dict:
        coordinator.register_worker(worker_id)
        return {"registered": worker_id}

    @app.post("/workers/{worker_id}/heartbeat")
    def heartbeat(worker_id: str) -> dict:
        coordinator.heartbeat(worker_id)
        return {"ok": True}

    @app.post("/workers/{worker_id}/lost")
    def lost(worker_id: str, body: LostBody) -> dict:
        coordinator.mark_worker_lost(worker_id, body.reason)
        return {"lost": worker_id}

    @app.post("/liveness/check")
    def liveness() -> dict:
        return {"newly_lost": sorted(coordinator.check_liveness())}

    @app.post("/rounds/begin")
    def begin_round(body: BeginRoundBody) -> dict:
        desc = coordinator.begin_round(body.participants, skipped_ids=body.skipped)
        return {
            "round_id": desc["round_id"],
            "generation": desc["generation"],
            "base_token": desc["base_token"],
            "participants": desc["participants"],
            "skipped": desc["skipped"],
            "bucket_count": desc["bucket_count"],
            "base_params": _params_to_json(desc["base_params"]),
        }

    @app.get("/rounds/current")
    def current_round() -> dict:
        desc = coordinator.round_descriptor()
        if desc is None:
            return {"open": False}
        out = {"open": desc["status"] == "open", **desc}
        out["base_params"] = _params_to_json(desc["base_params"])
        return out

    @app.post("/rounds/abort")
    def abort_round() -> dict:
        coordinator.abort_round()
        return {"aborted": True}

    @app.post("/rounds/skip/{worker_id}")
    def skip_round(worker_id: str) -> dict:
        coordinator.skip_round(worker_id)
        return {"skipped": worker_id}

    @app.post("/rounds/commit")
    def commit_round() -> dict:
        report = coordinator.commit_round()
        return {
            "outcome": report.outcome,
            "reason": report.reason,
            "record_id": report.record_id,
            "round_id": report.round_id,
            "generation_before": report.generation_before,
            "generation_after": report.generation_after,
            "bucket_evidence": [
                {
                    "bucket_index": e.bucket_index,
                    "generation": e.generation,
                    "contributors": list(e.contributors),
                    "samples_per_worker": list(e.samples_per_worker),
                    "total_samples": e.total_samples,
                    "reduced_norm": e.reduced_norm,
                    "reduced_hash12": e.reduced_hash12,
                    "weight_basis": e.weight_basis,
                    "cover_min": e.cover_min,
                    "cover_max": e.cover_max,
                    "sample_weight_min": e.sample_weight_min,
                }
                for e in report.bucket_evidence
            ],
            "detail": report.detail,
        }

    @app.post("/buckets/submit")
    def submit_bucket(body: SubmitBucketBody) -> dict:
        result = coordinator.submit_bucket(
            round_id=body.round_id,
            worker_id=body.worker_id,
            bucket_index=body.bucket_index,
            segment=np.asarray(body.segment, dtype=np.float64),
            n_samples=body.n_samples,
            base_token=body.base_token,
            present_mask=np.asarray(body.present_mask, dtype=bool),
        )
        return {
            "accepted": result.accepted,
            "reason": result.reason,
            "record_id": result.record_id,
            "bucket_index": result.bucket_index,
            "bucket_complete": result.bucket_complete,
            "all_buckets_received": result.all_buckets_received,
        }

    @app.get("/state")
    def state() -> dict:
        return {
            "generation": coordinator.current_generation(),
            "params": _params_to_json(coordinator.snapshot_params()),
        }

    @app.get("/diagnostics")
    def diagnostics() -> dict:
        return {
            "events": [
                {
                    "record_id": e.record_id,
                    "round_id": e.round_id,
                    "worker_id": e.worker_id,
                    "outcome": e.outcome,
                    "reason": e.reason,
                    "detail": e.detail,
                }
                for e in coordinator.diagnostics.events()
            ]
        }

    return app
