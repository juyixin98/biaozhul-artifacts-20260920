"""FastAPI application: architectures, datasets, jobs, SSE event stream."""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
from collections.abc import AsyncIterator

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import JSONResponse, StreamingResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from . import datasets as dsmod
from . import queue as q
from .config import get_settings
from .db import get_session, init_db
from .graph import GraphValidationError, build_model, normalize_spec, spec_hash
from .models import Architecture, Dataset, Event, Job, JobStatus
from .schemas import ArchitectureCreate, DatasetRegister, JobAction, JobCreate

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("nnlab")

settings = get_settings()
_worker_pool = None


@contextlib.asynccontextmanager
async def lifespan(app: FastAPI):
    global _worker_pool
    os.makedirs(settings.checkpoint_dir, exist_ok=True)
    for root in settings.whitelist:
        os.makedirs(root, exist_ok=True)
    init_db()
    if settings.worker_enabled:
        from .worker import WorkerPool

        _worker_pool = WorkerPool(settings)
        _worker_pool.start()
    try:
        yield
    finally:
        if _worker_pool is not None:
            _worker_pool.stop()


app = FastAPI(title="Local NN Training Workflow", version="1.0.0", lifespan=lifespan)


@app.exception_handler(GraphValidationError)
def _graph_error_handler(request: Request, exc: GraphValidationError):
    return JSONResponse(status_code=400, content={"detail": str(exc)})


def user_id(x_user_id: str | None = Header(default=None)) -> str:
    uid = (x_user_id or "anonymous").strip()
    if not uid or len(uid) > 100:
        raise HTTPException(status_code=400, detail="invalid X-User-Id")
    return uid


# ---------------------------------------------------------------- health


@app.get("/health")
def health():
    return {"status": "ok", "worker_enabled": settings.worker_enabled}


# ---------------------------------------------------------------- architectures


@app.post("/api/architectures", status_code=201)
def create_architecture(
    body: ArchitectureCreate,
    db: Session = Depends(get_session),
    uid: str = Depends(user_id),
):
    raw_layers = [l.model_dump(exclude_none=True) for l in body.layers]
    spec = {"input_features": body.input_features, "layers": raw_layers}
    try:
        spec = normalize_spec(spec)
        model = build_model(spec)
    except GraphValidationError:
        raise
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"invalid architecture: {exc}")

    digest = spec_hash(spec)
    existing = db.scalar(
        select(Architecture).where(Architecture.content_hash == digest)
    )
    if existing is not None:
        return _arch_dict(existing)

    arch = Architecture(
        name=body.name,
        spec=spec,
        content_hash=digest,
        param_count=sum(p.numel() for p in model.parameters()),
    )
    db.add(arch)
    db.commit()
    db.refresh(arch)
    return _arch_dict(arch)


@app.get("/api/architectures")
def list_architectures(db: Session = Depends(get_session)):
    rows = db.scalars(select(Architecture).order_by(Architecture.id)).all()
    return [_arch_dict(a) for a in rows]


@app.get("/api/architectures/{arch_id}")
def get_architecture(arch_id: int, db: Session = Depends(get_session)):
    arch = db.get(Architecture, arch_id)
    if arch is None:
        raise HTTPException(404, "architecture not found")
    return _arch_dict(arch)


def _arch_dict(a: Architecture) -> dict:
    return {
        "id": a.id,
        "name": a.name,
        "spec": a.spec,
        "content_hash": a.content_hash,
        "param_count": a.param_count,
        "created_at": a.created_at.isoformat(),
    }


# ---------------------------------------------------------------- datasets


@app.post("/api/datasets", status_code=201)
def register_dataset(
    body: DatasetRegister,
    db: Session = Depends(get_session),
    uid: str = Depends(user_id),
):
    try:
        info = dsmod.inspect_dataset(
            body.path, task=body.task, label_column=body.label_column,
            settings=settings,
        )
    except dsmod.DatasetError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    existing = db.scalar(
        select(Dataset).where(
            Dataset.path == info["resolved_path"], Dataset.digest == info["digest"]
        )
    )
    if existing is not None:
        return _ds_dict(existing)

    ds = Dataset(
        name=body.name,
        path=info["resolved_path"],
        fmt=info["fmt"],
        task=body.task,
        num_rows=info["num_rows"],
        num_features=info["num_features"],
        digest=info["digest"],
    )
    db.add(ds)
    db.commit()
    db.refresh(ds)
    return _ds_dict(ds)


@app.get("/api/datasets")
def list_datasets(db: Session = Depends(get_session)):
    rows = db.scalars(select(Dataset).order_by(Dataset.id)).all()
    return [_ds_dict(d) for d in rows]


def _ds_dict(d: Dataset) -> dict:
    return {
        "id": d.id,
        "name": d.name,
        "path": d.path,
        "format": d.fmt,
        "task": d.task,
        "num_rows": d.num_rows,
        "num_features": d.num_features,
        "digest": d.digest,
        "created_at": d.created_at.isoformat(),
    }


# ---------------------------------------------------------------- jobs


@app.post("/api/jobs", status_code=201)
def submit_job(
    body: JobCreate,
    db: Session = Depends(get_session),
    uid: str = Depends(user_id),
):
    try:
        job = q.create_job(
            db,
            user_id=uid,
            architecture_id=body.architecture_id,
            dataset_id=body.dataset_id,
            total_epochs=body.epochs,
            seed=body.seed,
            hyperparams=body.hyperparams,
            settings=settings,
        )
    except q.QueueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return _job_dict(job)


@app.get("/api/jobs")
def list_jobs(
    db: Session = Depends(get_session),
    uid: str = Depends(user_id),
    status: JobStatus | None = Query(default=None),
):
    stmt = select(Job).where(Job.user_id == uid).order_by(Job.id.desc())
    if status is not None:
        stmt = stmt.where(Job.status == status)
    rows = db.scalars(stmt).all()
    return [_job_dict(j) for j in rows]


@app.get("/api/jobs/{job_id}")
def get_job(job_id: int, db: Session = Depends(get_session),
            uid: str = Depends(user_id)):
    job = _owned_job(db, job_id, uid)
    return _job_dict(job)


@app.post("/api/jobs/{job_id}/action")
def job_action(
    job_id: int,
    body: JobAction,
    db: Session = Depends(get_session),
    uid: str = Depends(user_id),
):
    _owned_job(db, job_id, uid)
    try:
        if body.action == "pause":
            job = q.pause_job(db, job_id, uid)
        elif body.action == "resume":
            job = q.resume_job(db, job_id, uid)
        elif body.action == "cancel":
            job = q.cancel_job(db, job_id, uid)
        else:
            raise HTTPException(
                status_code=400,
                detail="action must be one of pause|resume|cancel",
            )
    except q.QueueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return _job_dict(job)


@app.get("/api/jobs/{job_id}/events")
def get_events(
    job_id: int,
    after_seq: int = Query(default=0, ge=0),
    db: Session = Depends(get_session),
    uid: str = Depends(user_id),
):
    _owned_job(db, job_id, uid)
    rows = q.list_events(db, job_id, after_seq)
    return {"events": [_event_dict(e) for e in rows]}


@app.get("/api/jobs/{job_id}/checkpoints")
def list_checkpoints(job_id: int, db: Session = Depends(get_session),
                     uid: str = Depends(user_id)):
    job = _owned_job(db, job_id, uid)
    return [
        {
            "id": c.id,
            "epoch": c.epoch,
            "path": c.path,
            "size_bytes": c.size_bytes,
            "valid": c.valid,
        }
        for c in sorted(job.checkpoints, key=lambda c: c.epoch, reverse=True)
    ]


# ---------------------------------------------------------------- SSE


@app.get("/api/jobs/{job_id}/events/stream")
async def stream_events(
    job_id: int,
    request: Request,
    after_seq: int = Query(default=0, ge=0),
    db: Session = Depends(get_session),
    uid: str = Depends(user_id),
):
    _owned_job(db, job_id, uid)

    async def event_gen() -> AsyncIterator[bytes]:
        seq = after_seq
        # Replay backlog first so reconnecting clients can fill every gap.
        while True:
            if await request.is_disconnected():
                break
            rows = q.list_events(db, job_id, seq)
            for e in rows:
                seq = e.seq
                yield _sse(f"event-{e.seq}", _event_dict(e), event=e.kind)
            if rows:
                status = rows[-1].payload.get("to") if rows[-1].kind == "status" else None
                if status in (s.value for s in (
                    JobStatus.COMPLETED, JobStatus.CANCELLED, JobStatus.FAILED,
                )):
                    yield _sse("end", {"status": status})
                    return
            else:
                job = db.get(Job, job_id)
                if job is not None and job.status in (
                    JobStatus.COMPLETED, JobStatus.CANCELLED, JobStatus.FAILED,
                ):
                    yield _sse("end", {"status": job.status.value})
                    return
            # Heartbeat comment keeps proxies from closing idle connections.
            yield b": ping\n\n"
            await asyncio.sleep(1.0)

    return StreamingResponse(
        event_gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


def _sse(id_: str, data: dict, event: str | None = None) -> bytes:
    out = f"id: {id_}\n"
    if event:
        out += f"event: {event}\n"
    out += "data: " + json.dumps(data, separators=(",", ":")) + "\n\n"
    return out.encode()


# ---------------------------------------------------------------- helpers


def _owned_job(db: Session, job_id: int, uid: str) -> Job:
    job = db.get(Job, job_id)
    if job is None or job.user_id != uid:
        raise HTTPException(404, "job not found")
    return job


def _event_dict(e: Event) -> dict:
    return {"seq": e.seq, "kind": e.kind, "payload": e.payload,
            "created_at": e.created_at.isoformat()}


def _job_dict(j: Job) -> dict:
    return {
        "id": j.id,
        "user_id": j.user_id,
        "architecture_id": j.architecture_id,
        "dataset_id": j.dataset_id,
        "status": j.status.value,
        "epochs_completed": j.epochs_completed,
        "total_epochs": j.total_epochs,
        "seed": j.seed,
        "hyperparams": j.hyperparams,
        "dataset_digest": j.dataset_digest,
        "split": j.split,
        "executor_id": j.executor_id,
        "lease_expires_at": (
            j.lease_expires_at.isoformat() if j.lease_expires_at else None
        ),
        "error": j.error,
        "created_at": j.created_at.isoformat(),
        "updated_at": j.updated_at.isoformat(),
    }
