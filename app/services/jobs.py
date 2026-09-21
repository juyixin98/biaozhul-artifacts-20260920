"""Architecture/dataset registration and job lifecycle transitions."""
from __future__ import annotations

import json

import numpy as np
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..models.network_spec import SpecError, parse_spec
from ..models.orm import Architecture, Dataset, Job, JobStatus
from . import data_safe
from .training import HyperParams, prepare_tensors, split_indices


class Conflict(Exception):
    """409-style domain error."""


class NotFound(LookupError):
    """404-style domain error."""


# -- architectures -----------------------------------------------------------

def create_architecture(db: Session, name: str, raw_spec: dict) -> Architecture:
    try:
        spec = parse_spec(raw_spec)
    except SpecError as exc:
        raise ValueError(str(exc)) from exc

    next_version = (
        db.scalar(
            select(func.coalesce(func.max(Architecture.version), 0)).where(
                Architecture.name == name
            )
        )
    ) + 1
    arch = Architecture(
        name=name,
        version=next_version,
        spec_json=spec.to_dict(),
        fingerprint=spec.fingerprint(),
        in_features=spec.in_features,
        out_features=spec.out_features,
    )
    db.add(arch)
    db.commit()
    db.refresh(arch)
    return arch


def get_architecture(db: Session, arch_id: str) -> Architecture:
    arch = db.get(Architecture, arch_id)
    if arch is None:
        raise NotFound(f"architecture {arch_id!r} not found")
    return arch


# -- datasets ----------------------------------------------------------------

def register_dataset(
    db: Session, feature_path: str, target_path: str | None, task: str
) -> Dataset:
    if task not in ("classification", "regression"):
        raise ValueError("task must be 'classification' or 'regression'")
    resolved_feat = data_safe.resolve_within_whitelist(feature_path)
    resolved_tgt = None
    if target_path:
        resolved_tgt = data_safe.resolve_within_whitelist(target_path)
    X, y = data_safe.load_dataset(feature_path, target_path)
    if X.shape[0] < 2:
        raise ValueError("dataset must contain at least 2 samples")
    if task == "classification":
        # Float targets are accepted only when integral; the stricter label
        # range check against architecture outputs happens at job creation.
        if (np.issubdtype(y.dtype, np.floating)
                and not np.all(y == y.astype("int64"))):
            raise ValueError("classification targets must be integer labels")
    summary = data_safe.summarize(X, y, resolved_feat, resolved_tgt, task)
    ds = Dataset(
        feature_path=str(resolved_feat),
        target_path=str(resolved_tgt) if resolved_tgt else None,
        task=task,
        summary_json=summary.to_dict(),
    )
    db.add(ds)
    db.commit()
    db.refresh(ds)
    return ds


def get_dataset(db: Session, dataset_id: str) -> Dataset:
    ds = db.get(Dataset, dataset_id)
    if ds is None:
        raise NotFound(f"dataset {dataset_id!r} not found")
    return ds


# -- jobs --------------------------------------------------------------------

def create_job(
    db: Session,
    user_id: str,
    architecture_id: str,
    dataset_id: str,
    hyperparams_raw: dict,
    epochs: int,
    seed: int,
    val_fraction: float,
) -> Job:
    arch = get_architecture(db, architecture_id)
    ds = get_dataset(db, dataset_id)
    hp = HyperParams.validate(hyperparams_raw)
    if not 1 <= epochs <= 10_000:
        raise ValueError("epochs must be in [1, 10000]")
    if not 0 <= seed < 2**31:
        raise ValueError("seed must be a non-negative 31-bit integer")

    spec = parse_spec(arch.spec_json)
    X, y = data_safe.load_dataset(ds.feature_path, ds.target_path)
    # Full shape/target validation up front (also re-run by the worker).
    prepare_tensors(spec, ds.task, X, y)
    if ds.summary_json["n_features"] != spec.in_features:
        raise ValueError(
            f"dataset feature width {ds.summary_json['n_features']} != "
            f"architecture input {spec.in_features}"
        )

    train_idx, val_idx = split_indices(X.shape[0], val_fraction, seed)
    # The bound contract records the dataset digest so a swapped file is
    # detectable by the worker before training.
    job = Job(
        user_id=user_id,
        architecture_id=arch.id,
        dataset_id=ds.id,
        hyperparams_json={"lr": hp.lr, "batch_size": hp.batch_size,
                          "weight_decay": hp.weight_decay},
        seed=seed,
        dataset_summary_json=ds.summary_json,
        train_idx_json=train_idx,
        val_idx_json=val_idx,
        status=JobStatus.QUEUED,
        epochs_total=epochs,
    )
    db.add(job)
    db.commit()
    db.refresh(job)
    return job


def get_job(db: Session, job_id: str, user_id: str | None = None) -> Job:
    job = db.get(Job, job_id)
    if job is None:
        raise NotFound(f"job {job_id!r} not found")
    if user_id is not None and job.user_id != user_id:
        raise NotFound(f"job {job_id!r} not found")
    return job


def _locked_job(db: Session, job_id: str, user_id: str) -> Job:
    job = db.scalar(select(Job).where(Job.id == job_id).with_for_update())
    if job is None:
        raise NotFound(f"job {job_id!r} not found")
    if job.user_id != user_id:
        raise NotFound(f"job {job_id!r} not found")
    return job


def request_pause(db: Session, job_id: str, user_id: str) -> Job:
    job = _locked_job(db, job_id, user_id)
    if job.status in (JobStatus.COMPLETED, JobStatus.CANCELLED, JobStatus.FAILED):
        raise Conflict(f"job is {job.status.value}; cannot pause")
    if job.status == JobStatus.PAUSED:
        db.commit()
        return job
    # QUEUED -> PAUSED immediately (resume returns it to QUEUED); RUNNING ->
    # PAUSED is observed by the executor at the next epoch boundary.
    if job.status == JobStatus.CANCELLING:
        raise Conflict("cancel already requested")
    job.status = JobStatus.PAUSED
    db.commit()
    db.refresh(job)
    return job


def request_resume(db: Session, job_id: str, user_id: str) -> Job:
    job = _locked_job(db, job_id, user_id)
    if job.status != JobStatus.PAUSED:
        raise Conflict(f"only paused jobs can resume (is {job.status.value})")
    job.status = JobStatus.QUEUED
    job.executor_id = None
    job.lease_expires_at = None
    job.heartbeat_at = None
    db.commit()
    db.refresh(job)
    return job


def request_cancel(db: Session, job_id: str, user_id: str) -> Job:
    job = _locked_job(db, job_id, user_id)
    if job.status in (JobStatus.COMPLETED, JobStatus.CANCELLED, JobStatus.FAILED):
        raise Conflict(f"job is {job.status.value}; cannot cancel")
    if job.status == JobStatus.CANCELLING:
        db.commit()
        return job
    if job.status == JobStatus.QUEUED or job.status == JobStatus.PAUSED:
        # Never started (or paused at boundary): terminal immediately.
        job.status = JobStatus.CANCELLED
        job.executor_id = None
        job.lease_expires_at = None
        from ..models.orm import utcnow
        job.finished_at = utcnow()
    else:
        job.status = JobStatus.CANCELLING  # runner observes at epoch boundary
    db.commit()
    db.refresh(job)
    return job
