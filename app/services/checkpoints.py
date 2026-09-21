"""Checkpoint durability: atomic write-then-publish, validation, rollback.

A checkpoint is written in three stages:

1. ``save_checkpoint`` writes to ``<job>/ckpt-<epoch>.tmp`` via torch.save and
   fsyncs the file.
2. It is hashed and atomically ``os.replace``d to its final ``.pt`` path
   (atomic on POSIX within one directory).
3. Only then is a ``Checkpoint`` row inserted ("published"). Readers never
   observe a file the database does not point at, and a crash between 1 and 2
   leaves only an ignored ``.tmp`` file.

On load, the digest is verified; a corrupt or unreadable checkpoint is marked
invalid, its row kept for diagnostics, and the most recent *valid* checkpoint
is used instead.
"""
from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from pathlib import Path

import torch
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import CHECKPOINT_DIR, MAX_CHECKPOINTS
from ..models.orm import Checkpoint, Job


class CheckpointError(Exception):
    pass


def job_dir(job_id: str) -> Path:
    d = CHECKPOINT_DIR / job_id
    d.mkdir(parents=True, exist_ok=True)
    return d


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def _fsync_dir(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def save_checkpoint(db: Session, job: Job, epoch: int, state: dict) -> Checkpoint:
    """Atomically persist ``state`` and publish the checkpoint row."""
    directory = job_dir(job.id)
    final_path = directory / f"ckpt-{epoch:05d}.pt"
    tmp_path = directory / f"ckpt-{epoch:05d}.tmp"

    # 1+2: durable temp file, then atomic rename.
    torch.save(state, tmp_path)
    digest = _sha256(tmp_path)
    with open(tmp_path, "rb+") as fh:
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp_path, final_path)
    _fsync_dir(directory)

    # 3: publish reference.
    row = Checkpoint(
        job_id=job.id,
        epoch=epoch,
        path=str(final_path),
        sha256=digest,
        size_bytes=final_path.stat().st_size,
        valid=True,
    )
    db.add(row)
    db.flush()
    _prune(db, job)
    db.commit()
    return row


def _prune(db: Session, job: Job) -> None:
    """Keep at most MAX_CHECKPOINTS valid checkpoints, newest first."""
    rows = list(
        db.scalars(
            select(Checkpoint)
            .where(Checkpoint.job_id == job.id, Checkpoint.valid.is_(True))
            .order_by(Checkpoint.epoch.desc(), Checkpoint.id.desc())
        )
    )
    for old in rows[MAX_CHECKPOINTS:]:
        try:
            Path(old.path).unlink(missing_ok=True)
        except OSError:
            pass
        db.delete(old)


def _row_is_intact(row: Checkpoint) -> bool:
    p = Path(row.path)
    if not p.is_file():
        return False
    try:
        if _sha256(p) != row.sha256:
            return False
        # torch must also be able to unpickle it.
        torch.load(p, map_location="cpu", weights_only=False)
    except Exception:
        return False
    return True


def latest_valid_checkpoint(db: Session, job_id: str) -> Checkpoint | None:
    """Return the newest checkpoint that is loadable; mark corrupt ones.

    Rows are walked newest-first. A corrupt row is flagged ``valid=False`` (its
    file retained) and we roll back to the next-most-recent valid one.
    """
    rows = list(
        db.scalars(
            select(Checkpoint)
            .where(Checkpoint.job_id == job_id)
            .order_by(Checkpoint.epoch.desc(), Checkpoint.id.desc())
        )
    )
    changed = False
    fallback: Checkpoint | None = None
    for row in rows:
        if not row.valid:
            continue
        if _row_is_intact(row):
            fallback = row
            break
        row.valid = False  # corrupt on disk -> rollback
        changed = True
    if changed:
        db.commit()
    return fallback


def load_state(row: Checkpoint) -> dict:
    p = Path(row.path)
    if _sha256(p) != row.sha256:
        raise CheckpointError(f"checkpoint {p} failed integrity check")
    return torch.load(p, map_location="cpu", weights_only=False)
