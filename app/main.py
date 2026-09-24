"""FastAPI application: immutable robot-experiment snapshot service.

Run locally:

    uvicorn app.main:app --reload
    # or: python -m app.main   (host/port via HOST/PORT env vars)

State directory: ``SNAPSHOT_ROOT`` env var (default ``./data``).
"""

from __future__ import annotations

import json
import os
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import JSONResponse, PlainTextResponse

from .models import (
    BagSummary,
    CalibrationInput,
    CalibrationView,
    CreateJobRequest,
    CreateSnapshotRequest,
    IndexPublishRequest,
    IndexView,
    JobView,
    SnapshotView,
)
from .crypto import digest_json
from .storage import Conflict, NotFound, Storage, StorageError

state: dict[str, Storage] = {}


@asynccontextmanager
async def lifespan(app: FastAPI) -> Any:
    root = os.environ.get("SNAPSHOT_ROOT", os.path.join(os.getcwd(), "data"))
    store = Storage(root)
    recovered = store.recover_after_restart()
    state["store"] = store
    state["recovered_at_boot"] = recovered
    yield
    state.clear()


app = FastAPI(
    title="Robot Experiment Snapshot Service",
    version="1.0.0",
    description="Reproducible, immutable offline robot-experiment snapshots "
    "(bag summary + params + calibration + algorithm version).",
    lifespan=lifespan,
)


def store() -> Storage:
    return state["store"]


@app.exception_handler(NotFound)
async def _nf(_request: Any, exc: NotFound) -> JSONResponse:
    return JSONResponse(status_code=404, content={"detail": str(exc)})


@app.exception_handler(Conflict)
async def _conflict(_request: Any, exc: Conflict) -> JSONResponse:
    # 409 covers failed pre-publication validation and signature problems.
    return JSONResponse(status_code=409, content={"detail": str(exc)})


@app.exception_handler(StorageError)
async def _se(_request: Any, exc: StorageError) -> JSONResponse:
    return JSONResponse(status_code=500, content={"detail": str(exc)})


@app.get("/health")
def health() -> dict[str, Any]:
    return {"status": "ok", "recovered_at_boot": state.get("recovered_at_boot", [])}


@app.get("/public-key", response_class=PlainTextResponse)
def public_key() -> str:
    return store().signer.public_pem()


# ---------------------------------------------------------------------- bags
@app.post("/bags", status_code=201)
async def upload_bag(
    file: UploadFile = File(..., description="synthetic bag bytes"),
    summary: str = Form(..., description="BagSummary as JSON"),
) -> dict[str, Any]:
    try:
        summary_obj = BagSummary.model_validate_json(summary).model_dump()
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=f"invalid summary: {exc}")
    data = await file.read()
    if not data:
        raise HTTPException(status_code=422, detail="empty bag file")
    return store().add_bag(data, summary_obj)


@app.get("/bags")
def list_bags() -> list[dict[str, Any]]:
    return store().list_bags()


# --------------------------------------------------------------- calibrations
@app.post("/calibrations", status_code=201, response_model=CalibrationView)
def publish_calibration(calib: CalibrationInput) -> dict[str, Any]:
    return store().add_calibration(calib.model_dump())


@app.get("/calibrations", response_model=list[CalibrationView])
def list_calibrations() -> list[dict[str, Any]]:
    return store().list_calibrations()


@app.get("/calibrations/{calib_id}", response_model=CalibrationView)
def get_calibration(calib_id: str) -> dict[str, Any]:
    try:
        return store().get_calibration(calib_id)
    except NotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc))


# ----------------------------------------------------------------- snapshots
@app.post("/snapshots", status_code=201, response_model=SnapshotView)
def create_snapshot(req: CreateSnapshotRequest) -> dict[str, Any]:
    try:
        return store().create_snapshot(req.model_dump())
    except NotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc))


@app.get("/snapshots", response_model=list[SnapshotView])
def list_snapshots() -> list[dict[str, Any]]:
    return store().list_snapshots()


@app.get("/snapshots/{snap_id}", response_model=SnapshotView)
def get_snapshot(snap_id: str) -> dict[str, Any]:
    return store().get_snapshot(snap_id)


@app.get("/snapshots/{snap_id}/verify")
def verify_snapshot(snap_id: str) -> dict[str, Any]:
    snap = store().get_snapshot(snap_id)
    return {
        "snapshot_id": snap_id,
        "signature_valid": store().snapshot_signature_valid(snap),
        "id_matches_manifest": snap_id
        == __import__("app.crypto", fromlist=["digest_json"]).digest_json(snap["manifest"]),
    }


# ---------------------------------------------------------------------- jobs
@app.post("/jobs", status_code=202, response_model=JobView)
def start_job(req: CreateJobRequest) -> dict[str, Any]:
    """Start a run in the background. Poll GET /jobs/{id} for completion."""
    return store().start_job(req.model_dump())


@app.post("/jobs/run-sync", status_code=201, response_model=JobView)
def run_job_sync(req: CreateJobRequest) -> dict[str, Any]:
    """Convenience endpoint: execute synchronously and return the final state."""
    return store().create_job(req.model_dump())


@app.get("/jobs", response_model=list[JobView])
def list_jobs(snapshot_id: str | None = None) -> list[dict[str, Any]]:
    return store().list_jobs(snapshot_id)


@app.get("/jobs/{job_id}", response_model=JobView)
def get_job(job_id: str) -> dict[str, Any]:
    return store().get_job(job_id)


@app.get("/jobs/{job_id}/artifacts/result.json")
def get_job_artifact(job_id: str) -> JSONResponse:
    job = store().get_job(job_id)
    if not job["output_path"] or not os.path.exists(job["output_path"]):
        raise HTTPException(status_code=404, detail="artifact missing (evidence absent)")
    with open(job["output_path"], "rb") as fh:
        raw = fh.read()
    try:
        return JSONResponse(content=json.loads(raw))
    except json.JSONDecodeError:
        # Artifact exists but is no longer valid JSON — return raw bytes honestly.
        return JSONResponse(
            status_code=409,
            content={"detail": "artifact on disk is not valid JSON; it has been tampered with"},
        )


@app.post("/jobs/{job_id}/revalidate", response_model=JobView)
def revalidate_job(job_id: str) -> dict[str, Any]:
    return store().revalidate_job(job_id)


@app.post("/jobs/{job_id}/simulate-tamper", response_model=JobView)
def simulate_tamper(job_id: str, mode: str = "swap") -> dict[str, Any]:
    """Demo-only fault injection: mode='swap' (replace evidence) or 'missing'."""
    if mode not in ("swap", "missing"):
        raise HTTPException(status_code=422, detail="mode must be 'swap' or 'missing'")
    return store().simulate_tamper_output(job_id, mode)


# ------------------------------------------------------------------- indexes
@app.post("/indexes", status_code=201, response_model=IndexView)
def publish_index(req: IndexPublishRequest) -> dict[str, Any]:
    try:
        return store().publish_index(req.job_ids)
    except (NotFound, Conflict) as exc:
        # Validation failure -> nothing published. 409 with the exact reasons.
        status_code = 404 if isinstance(exc, NotFound) else 409
        raise HTTPException(status_code=status_code, detail=str(exc))


@app.get("/indexes", response_model=list[IndexView])
def list_indexes() -> list[dict[str, Any]]:
    return store().list_indexes()


@app.get("/indexes/latest", response_model=IndexView)
def latest_index() -> dict[str, Any]:
    idx = store().latest_index()
    if idx is None:
        raise HTTPException(status_code=404, detail="no indexes published yet")
    return idx


@app.get("/indexes/{index_id}", response_model=IndexView)
def get_index(index_id: str) -> dict[str, Any]:
    return store().get_index(index_id)


@app.get("/indexes/{index_id}/verify")
def verify_index(index_id: str) -> dict[str, Any]:
    return store().verify_index(index_id)


def main() -> None:  # pragma: no cover
    import uvicorn

    uvicorn.run(
        "app.main:app",
        host=os.environ.get("HOST", "127.0.0.1"),
        port=int(os.environ.get("PORT", "8000")),
        reload=False,
    )


if __name__ == "__main__":  # pragma: no cover
    main()
