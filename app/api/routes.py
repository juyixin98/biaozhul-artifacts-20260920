"""HTTP API."""
from __future__ import annotations

import asyncio
import json

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from fastapi.responses import StreamingResponse
from starlette.concurrency import run_in_threadpool
from sqlalchemy.orm import Session

from ..db import SessionLocal, get_session
from ..models.orm import JobStatus
from ..services import events, jobs
from .schemas import (
    ArchitectureIn,
    ArchitectureOut,
    DatasetIn,
    DatasetOut,
    EventOut,
    JobIn,
    JobOut,
)

router = APIRouter()

_TERMINAL = {JobStatus.COMPLETED, JobStatus.CANCELLED, JobStatus.FAILED}


def _fetch_events(job_id: str, after: int) -> list[dict]:
    """Read events on a fresh session (called from a worker thread)."""
    s = SessionLocal()
    try:
        return [
            {"seq": r.seq, "kind": r.kind, "payload": r.payload_json}
            for r in events.read_events(s, job_id, after)
        ]
    finally:
        s.close()


def user_id(x_user_id: str | None = Header(default=None)) -> str:
    # Minimal per-user identity: a required header keeps the demo/test surface
    # trivial while still partitioning the 3-running-jobs quota.
    if not x_user_id or not x_user_id.strip():
        raise HTTPException(status_code=401, detail="X-User-Id header required")
    if len(x_user_id) > 200:
        raise HTTPException(status_code=400, detail="X-User-Id too long")
    return x_user_id.strip()


def _job_out(job) -> JobOut:
    return JobOut(
        id=job.id,
        user_id=job.user_id,
        architecture_id=job.architecture_id,
        dataset_id=job.dataset_id,
        status=job.status.value,
        epochs_total=job.epochs_total,
        epochs_done=job.epochs_done,
        seed=job.seed,
        hyperparams=job.hyperparams_json,
        dataset_summary=job.dataset_summary_json,
        error=job.error,
        lease_expires_at=job.lease_expires_at,
        heartbeat_at=job.heartbeat_at,
        created_at=job.created_at,
        finished_at=job.finished_at,
    )


# -- architectures -----------------------------------------------------------

@router.post("/architectures", response_model=ArchitectureOut, status_code=201)
def publish_architecture(body: ArchitectureIn, db: Session = Depends(get_session)):
    try:
        arch = jobs.create_architecture(db, body.name, body.spec)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return ArchitectureOut(
        id=arch.id, name=arch.name, version=arch.version,
        fingerprint=arch.fingerprint, in_features=arch.in_features,
        out_features=arch.out_features, spec=arch.spec_json,
    )


@router.get("/architectures/{arch_id}", response_model=ArchitectureOut)
def read_architecture(arch_id: str, db: Session = Depends(get_session)):
    try:
        arch = jobs.get_architecture(db, arch_id)
    except jobs.NotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return ArchitectureOut(
        id=arch.id, name=arch.name, version=arch.version,
        fingerprint=arch.fingerprint, in_features=arch.in_features,
        out_features=arch.out_features, spec=arch.spec_json,
    )


# -- datasets ----------------------------------------------------------------

@router.post("/datasets", response_model=DatasetOut, status_code=201)
def register_dataset(body: DatasetIn, db: Session = Depends(get_session)):
    try:
        ds = jobs.register_dataset(
            db, body.feature_path, body.target_path, body.task)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001 - path/parse errors -> 4xx
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return DatasetOut(
        id=ds.id, feature_path=ds.feature_path, target_path=ds.target_path,
        task=ds.task, summary=ds.summary_json,
    )


# -- jobs --------------------------------------------------------------------

@router.post("/jobs", response_model=JobOut, status_code=201)
def create_job(body: JobIn, db: Session = Depends(get_session),
               user: str = Depends(user_id)):
    try:
        job = jobs.create_job(
            db, user, body.architecture_id, body.dataset_id,
            body.hyperparams, body.epochs, body.seed, body.val_fraction)
    except jobs.NotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return _job_out(job)


@router.get("/jobs/{job_id}", response_model=JobOut)
def read_job(job_id: str, db: Session = Depends(get_session),
             user: str = Depends(user_id)):
    try:
        job = jobs.get_job(db, job_id, user)
    except jobs.NotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return _job_out(job)


@router.post("/jobs/{job_id}/pause", response_model=JobOut)
def pause_job(job_id: str, db: Session = Depends(get_session),
              user: str = Depends(user_id)):
    try:
        return _job_out(jobs.request_pause(db, job_id, user))
    except jobs.NotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except jobs.Conflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/jobs/{job_id}/resume", response_model=JobOut)
def resume_job(job_id: str, db: Session = Depends(get_session),
               user: str = Depends(user_id)):
    try:
        return _job_out(jobs.request_resume(db, job_id, user))
    except jobs.NotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except jobs.Conflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/jobs/{job_id}/cancel", response_model=JobOut)
def cancel_job(job_id: str, db: Session = Depends(get_session),
               user: str = Depends(user_id)):
    try:
        return _job_out(jobs.request_cancel(db, job_id, user))
    except jobs.NotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except jobs.Conflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.get("/jobs/{job_id}/events", response_model=list[EventOut])
def list_events(job_id: str, after: int = 0, db: Session = Depends(get_session),
                user: str = Depends(user_id)):
    try:
        jobs.get_job(db, job_id, user)
    except jobs.NotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    rows = events.read_events(db, job_id, after)
    return [
        EventOut(seq=r.seq, kind=r.kind, payload=r.payload_json,
                 created_at=r.created_at)
        for r in rows
    ]


@router.get("/jobs/{job_id}/events/stream")
async def stream_events(job_id: str, request: Request,
                        db: Session = Depends(get_session),
                        user: str = Depends(user_id)):
    """SSE feed.

    Events carry their per-job sequence number as ``id:``. A reconnecting
    client supplies ``Last-Event-ID`` (or ``?after=N``) and receives every
    missed event — replay does not re-run training.
    """
    try:
        jobs.get_job(db, job_id, user)
    except jobs.NotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    last = request.query_params.get("after")
    if last is None:
        last = request.headers.get("last-event-id")
    last_id = int(last) if last and last.isdigit() else 0

    async def gen():
        seen = last_id
        while True:
            if await request.is_disconnected():
                break
            # Sync ORM read on a short-lived session, off the event loop so a
            # polling SSE client never blocks other requests.
            rows = await run_in_threadpool(_fetch_events, job_id, seen)
            terminal = False
            for r in rows:
                data = json.dumps(
                    {"seq": r["seq"], "kind": r["kind"],
                     "payload": r["payload"]},
                    default=str,
                )
                yield f"id: {r['seq']}\nevent: {r['kind']}\ndata: {data}\n\n"
                seen = r["seq"]
                if r["kind"] in ("completed", "cancelled", "failed"):
                    terminal = True
            if terminal:
                break
            await asyncio.sleep(0.3)

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
