"""Checkpoint persistence.

Durability protocol (see requirement: "先原子落盘再发布引用"):
  1. write to ``ckpt-<epoch>.tmp.<pid>`` next to the final path;
  2. flush + fsync the file;
  3. ``os.replace`` into ``ckpt-<epoch>.pt`` (atomic rename);
  4. fsync the directory;
  5. only now insert the CheckpointRef row (the "publish" step).

On load, every candidate is verified (torch.load + torch gen_state dict +
sha256 sidecar not used; instead a stored magic/epoch plus a round-trip
torch.load). Corrupt or unverifiable checkpoints are marked invalid and the
loader falls back to the most recent *valid* one.
"""
from __future__ import annotations

import os
from dataclasses import dataclass

import torch

from .config import get_settings

CKPT_MAGIC = "NNLAB-CKPT-V1"
KEEP_LAST_N = 3


class CheckpointError(RuntimeError):
    pass


def job_dir(job_id: int) -> str:
    base = os.path.join(get_settings().checkpoint_dir, f"job-{job_id}")
    os.makedirs(base, exist_ok=True)
    return base


def ckpt_path(job_id: int, epoch: int) -> str:
    return os.path.join(job_dir(job_id), f"ckpt-{epoch:05d}.pt")


def save_checkpoint(
    *,
    job_id: int,
    epoch: int,
    model: torch.nn.Module,
    optimizer: torch.optim.Optimizer,
    rng_state: tuple,
    completed_epoch: int,
) -> str:
    """Atomically persist checkpoint state and return its final path."""
    final = ckpt_path(job_id, epoch)
    tmp = f"{final}.tmp.{os.getpid()}"
    payload = {
        "magic": CKPT_MAGIC,
        "epoch": epoch,
        "completed_epoch": completed_epoch,
        "model_state": model.state_dict(),
        "optimizer_state": optimizer.state_dict(),
        "rng_state": rng_state,
    }
    with open(tmp, "wb") as f:
        torch.save(payload, f)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, final)
    _fsync_dir(os.path.dirname(final))
    return final


def _fsync_dir(path: str) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


@dataclass
class LoadedCheckpoint:
    epoch: int
    completed_epoch: int
    payload: dict


def is_valid_file(path: str, expected_epoch: int | None = None) -> bool:
    """Verify a checkpoint file is complete and loadable."""
    try:
        if not os.path.isfile(path) or os.path.getsize(path) == 0:
            return False
        payload = torch.load(path, map_location="cpu", weights_only=False)
        if not isinstance(payload, dict):
            return False
        if payload.get("magic") != CKPT_MAGIC:
            return False
        if not isinstance(payload.get("model_state"), dict):
            return False
        if not isinstance(payload.get("optimizer_state"), dict):
            return False
        if "rng_state" not in payload or "completed_epoch" not in payload:
            return False
        if expected_epoch is not None and payload.get("epoch") != expected_epoch:
            return False
        return True
    except Exception:
        return False


def load_checkpoint(path: str, expected_epoch: int | None = None) -> LoadedCheckpoint:
    if not is_valid_file(path, expected_epoch):
        raise CheckpointError(f"checkpoint is missing or corrupt: {path}")
    payload = torch.load(path, map_location="cpu", weights_only=False)
    return LoadedCheckpoint(
        epoch=int(payload["epoch"]),
        completed_epoch=int(payload["completed_epoch"]),
        payload=payload,
    )


def latest_valid(refs: list) -> object | None:
    """Return the newest DB checkpoint ref whose file still validates.

    Invalid refs are mutated in place (``.valid = False``) by the caller's
    session; this function only classifies. Ties broken by epoch desc.
    """
    for ref in sorted(refs, key=lambda r: r.epoch, reverse=True):
        if not ref.valid:
            continue
        # Fallback validation checks integrity, not the epoch marker: the
        # whole point is to fall back to an earlier epoch's checkpoint.
        if is_valid_file(ref.path):
            return ref
    return None


def prune_paths(refs: list, keep: int = KEEP_LAST_N) -> list:
    """Return refs older than the newest ``keep`` (candidates for removal)."""
    ordered = sorted(refs, key=lambda r: r.epoch, reverse=True)
    return ordered[keep:]
