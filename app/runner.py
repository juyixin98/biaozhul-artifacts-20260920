"""CPU training executor.

Determinism contract
--------------------
Fixed seed + resume must match an uninterrupted run within the declared
tolerance (see README). We guarantee it by:

  * seeding torch/numpy/python RNGs **before** the model is built, so weight
    init is identical for a given seed;
  * shuffling each epoch's batches with a per-epoch ``torch.Generator``
    seeded from ``(seed, epoch)`` — resume from epoch N reproduces epoch N
    exactly regardless of what RNG calls happened before it;
  * checkpoints storing model, optimizer and all three RNG states plus the
    completed position (epoch).
"""
from __future__ import annotations

import logging
import os
import random
from typing import Any

import numpy as np
import torch
import torch.nn as nn
from sqlalchemy.orm import Session

from . import checkpoints as ck
from . import datasets as dsmod
from . import queue as q
from .config import Settings
from .graph import build_model
from .models import Job, JobStatus

log = logging.getLogger("nnlab.runner")

# Heartbeat every N training batches.
_HEARTBEAT_BATCHES = 20

# Test-only hook registry: job_id -> callable(job_id, epoch, status) invoked
# after a successful epoch commit, inside the runner thread. Lets tests
# pause/cancel deterministically at an epoch boundary. Empty in production.
TEST_HOOKS: dict[int, object] = {}


def _capture_rng() -> dict[str, Any]:
    return {
        "torch": torch.get_rng_state(),
        "numpy": np.random.get_state(),
        "python": random.getstate(),
    }


def _restore_rng(state: dict[str, Any]) -> None:
    """Restore RNG states saved via torch.save.

    The python-random tuple serialises its internal state as a list; restore
    it to the tuple shape ``random.setstate`` expects.
    """
    torch.set_rng_state(state["torch"])
    np.random.set_state(tuple(state["numpy"]))
    version, internal, gauss = state["python"]
    if isinstance(internal, list):
        internal = tuple(internal)
    random.setstate((version, internal, gauss))


def _set_all_seeds(seed: int) -> None:
    random.seed(seed)
    np.random.seed(seed)
    torch.manual_seed(seed)


def run_job(db: Session, job: Job, *, settings: Settings) -> str:
    """Execute a claimed/resumed job until it pauses, cancels or finishes.

    Returns the final status value. Assumes the caller has already claimed
    the job (RUNNING + valid lease + executor_id).
    """
    executor_id = job.executor_id
    dataset = job.dataset

    info = dsmod.inspect_dataset(dataset.path, task=dataset.task, settings=settings)
    dsmod.verify_digest(info["resolved_path"], job.dataset_digest)
    x, y = info["x"], info["y"]

    split = job.split
    train_idx = np.asarray(split["train"], dtype=np.int64)
    val_idx = np.asarray(split["val"], dtype=np.int64)
    x_train, y_train = x[train_idx], y[train_idx]
    x_val, y_val = x[val_idx], y[val_idx]

    task = dataset.task
    spec = job.architecture.spec

    # Seed BEFORE model construction so weight init is reproducible.
    _set_all_seeds(job.seed)
    old_threads = torch.get_num_threads()
    torch.set_num_threads(1)  # bitwise-stable CPU reductions
    try:
        model = build_model(spec)
        hp = job.hyperparams
        optimizer = torch.optim.SGD(
            model.parameters(), lr=hp["lr"], weight_decay=hp["weight_decay"]
        )
        loss_fn: nn.Module = (
            nn.CrossEntropyLoss() if task == "classification" else nn.MSELoss()
        )

        start_epoch = job.epochs_completed + 1

        # Resume from the newest VALID, loadable checkpoint. Corrupt published
        # refs are marked invalid and the loader falls back to the last good.
        refs = list(job.checkpoints)
        good = ck.latest_valid(refs)
        corrupt = [
            r for r in refs
            if r.valid and not ck.is_valid_file(r.path)
        ]
        for ref in corrupt:
            ref.valid = False
            q.append_event_fenced(
                db, job.id, executor_id, "log",
                {"message": f"checkpoint at epoch {ref.epoch} is corrupt; "
                            "marked invalid"},
            )
        if good is not None:
            loaded = ck.load_checkpoint(good.path, expected_epoch=good.epoch)
            model.load_state_dict(loaded.payload["model_state"])
            optimizer.load_state_dict(loaded.payload["optimizer_state"])
            try:
                _restore_rng(loaded.payload["rng_state"])
            except Exception:
                # Per-epoch generators are self-seeding, so training itself
                # stays deterministic even if global RNG restore fails.
                _set_all_seeds(job.seed)
            start_epoch = loaded.completed_epoch + 1
            q.append_event_fenced(
                db, job.id, executor_id, "log",
                {"message": f"resumed from epoch {loaded.completed_epoch} "
                            f"checkpoint ({os.path.basename(good.path)})"},
            )

        batch_size = hp["batch_size"]
        final_status = JobStatus.RUNNING.value

        for epoch in range(start_epoch, job.total_epochs + 1):
            # Reflect status transitions made by other sessions (pause /
            # cancel / lease takeover) before the boundary control check.
            db.refresh(job)
            # Control check at the epoch boundary even when an epoch has too
            # few batches for a mid-epoch heartbeat. A pause/cancel request
            # placed before this point is honoured before any more work.
            boundary = _control_state(db, job.id, executor_id)
            if boundary in ("paused", "cancelled"):
                # Request landed between epochs. pause_job/cancel_job already
                # recorded the status transition; just drop our lease token
                # and stop (no duplicate status event).
                target = (
                    JobStatus.PAUSED if boundary == "paused"
                    else JobStatus.CANCELLED
                )
                try:
                    db.query(Job).filter(
                        Job.id == job.id, Job.executor_id == executor_id
                    ).update({"executor_id": None, "lease_expires_at": None})
                    db.commit()
                except Exception:
                    db.rollback()
                return target.value
            if boundary == "lost":
                log.info("job %s lost its lease before epoch %d", job.id, epoch)
                return JobStatus.RUNNING.value

            # Fresh per-epoch permutation from (seed, epoch): resume is
            # bitwise independent of prior RNG consumption.
            epoch_gen = torch.Generator()
            epoch_gen.manual_seed(int(job.seed) * 1_000_003 + epoch)

            model.train()
            perm = torch.randperm(len(x_train), generator=epoch_gen)
            total = 0.0
            n_seen = 0
            stop_after_epoch = False
            for b, start in enumerate(range(0, len(x_train), batch_size)):
                idx = perm[start : start + batch_size]
                xb = torch.from_numpy(x_train[idx.numpy()])
                yb = torch.from_numpy(y_train[idx.numpy()])
                optimizer.zero_grad()
                pred = model(xb)
                target = yb.long() if task == "classification" else yb
                if task != "classification":
                    pred = pred.reshape(-1)
                loss = loss_fn(pred, target)
                loss.backward()
                optimizer.step()
                total += float(loss.detach()) * xb.shape[0]
                n_seen += xb.shape[0]

                if (b + 1) % _HEARTBEAT_BATCHES == 0:
                    state = _renew_or_status(db, job.id, executor_id)
                    if state == "lost":
                        log.info("job %s lost its lease mid-epoch; aborting", job.id)
                        return JobStatus.RUNNING.value
                    if state in ("paused", "cancelled"):
                        # Honour the epoch boundary: finish this epoch, then
                        # commit (which records the terminal/paused state).
                        stop_after_epoch = True

            train_loss = total / max(n_seen, 1)
            val_loss, val_acc = _evaluate(model, x_val, y_val, task, loss_fn)

            # Atomic file FIRST, publish reference + metrics under fence.
            path = ck.save_checkpoint(
                job_id=job.id,
                epoch=epoch,
                model=model,
                optimizer=optimizer,
                rng_state=_capture_rng(),
                completed_epoch=epoch,
            )
            metrics = {"train_loss": train_loss, "val_loss": val_loss}
            if task == "classification":
                metrics["val_accuracy"] = val_acc

            final_status = q.commit_epoch(
                db,
                job_id=job.id,
                executor_id=executor_id,
                epoch=epoch,
                metrics=metrics,
                checkpoint_path=path,
                checkpoint_size=os.path.getsize(path),
            )
            hook = TEST_HOOKS.get(job.id)
            if hook is not None:
                # Hooks remove themselves once their condition fires; other
                # epochs keep the same hook registered.
                consumed = hook(job.id, epoch, final_status)
                if consumed is True or final_status != "running":
                    TEST_HOOKS.pop(job.id, None)
            if final_status != "running" or stop_after_epoch:
                break

        return final_status
    except q.LeaseLost:
        log.warning("job %s fenced out (lease lost)", job.id)
        return JobStatus.RUNNING.value
    except Exception as exc:
        log.exception("job %s failed", job.id)
        detail = f"{type(exc).__name__}: {exc}"
        try:
            q.mark_failed(
                db, job_id=job.id, executor_id=executor_id,
                error=detail,
            )
        except q.LeaseLost:
            pass
        return JobStatus.FAILED.value
    finally:
        torch.set_num_threads(old_threads)


def _evaluate(model, x_val, y_val, task: str, loss_fn) -> tuple[float, float]:
    model.eval()
    with torch.no_grad():
        xv = torch.from_numpy(x_val)
        yv = torch.from_numpy(y_val)
        pred = model(xv)
        if task == "classification":
            loss = float(loss_fn(pred, yv.long()))
            acc = float((pred.argmax(dim=1) == yv.long()).float().mean())
            return loss, acc
        loss = float(loss_fn(pred.reshape(-1), yv))
        return loss, 0.0


def _renew_or_status(db: Session, job_id: int, executor_id: str) -> str:
    """Renew the lease; return one of: ok | paused | cancelled | lost."""
    try:
        q.heartbeat(db, job_id, executor_id)
        return "ok"
    except q.LeaseLost:
        db.rollback()
        return _control_state(db, job_id, executor_id)


def _control_state(db: Session, job_id: int, executor_id: str) -> str:
    """Classify current job control state from an executor's point of view.

    If our lease fencing token is still in place, a PAUSED/CANCELLED status
    is a pending request; otherwise the lease was lost (possibly reclaimed).
    """
    job = db.get(Job, job_id)
    if job is None:
        return "lost"
    if job.executor_id != executor_id or job.status != JobStatus.RUNNING:
        if job.status == JobStatus.PAUSED:
            return "paused"
        if job.status == JobStatus.CANCELLED:
            return "cancelled"
        return "lost"
    return "ok"
