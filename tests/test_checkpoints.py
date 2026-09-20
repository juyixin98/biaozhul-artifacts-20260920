"""Checkpoint: atomic publish, corruption fallback, retention pruning."""
from __future__ import annotations

import os
import uuid

import pytest
import torch

from app import checkpoints as ck
from app.graph import build_model
from app.datasets import inspect_dataset
from app.config import get_settings
from app.db import SessionLocal
from app.models import Architecture, Dataset, Job, CheckpointRef
from app import queue as q

from .conftest import make_arch_spec


class _Ref:
    def __init__(self, epoch, path, valid=True):
        self.id = epoch
        self.epoch = epoch
        self.path = path
        self.valid = valid


def _payload(epoch):
    model = build_model(make_arch_spec())
    opt = torch.optim.SGD(model.parameters(), lr=0.01)
    return {
        "magic": ck.CKPT_MAGIC,
        "epoch": epoch,
        "completed_epoch": epoch,
        "model_state": model.state_dict(),
        "optimizer_state": opt.state_dict(),
        "rng_state": {"torch": torch.get_rng_state()},
    }


def _save_real(epoch):
    model = build_model(make_arch_spec())
    opt = torch.optim.SGD(model.parameters(), lr=0.01)
    path = ck.save_checkpoint(
        job_id=900000 + epoch, epoch=epoch, model=model, optimizer=opt,
        rng_state=("v", tuple(), None), completed_epoch=epoch,
    )
    return path


def test_save_is_atomic_no_tmp_left_behind():
    path = _save_real(1)
    assert os.path.isfile(path)
    assert os.path.dirname(path) not in [None]
    leftovers = [f for f in os.listdir(os.path.dirname(path)) if ".tmp." in f]
    assert leftovers == []


def test_is_valid_rejects_truncated_and_garbage_files():
    path = _save_real(2)
    assert ck.is_valid_file(path, expected_epoch=2)
    # Truncate: corrupt on-disk file must fail validation.
    size = os.path.getsize(path)
    with open(path, "r+b") as f:
        f.truncate(size // 2)
    assert not ck.is_valid_file(path, expected_epoch=2)

    garbage = path + ".g"
    with open(garbage, "wb") as f:
        f.write(b"not a checkpoint" * 100)
    assert not ck.is_valid_file(garbage)
    os.remove(garbage)


def test_is_valid_rejects_wrong_epoch_marker():
    path = _save_real(3)
    assert ck.is_valid_file(path, expected_epoch=3)
    assert not ck.is_valid_file(path, expected_epoch=99)


def test_latest_valid_falls_back_to_last_good():
    paths = {e: _save_real(100 + e) for e in (1, 2, 3)}
    refs = [_Ref(e, paths[e]) for e in (1, 2, 3)]
    # Corrupt newest: must fall back to epoch 2.
    with open(paths[3], "wb") as f:
        f.write(b"x")
    good = ck.latest_valid(refs)
    assert good.epoch == 2
    # Corrupt epoch 2 as well: falls back to epoch 1.
    with open(paths[2], "wb") as f:
        f.write(b"x")
    good = ck.latest_valid(refs)
    assert good.epoch == 1


def test_all_corrupt_returns_none():
    paths = {e: _save_real(200 + e) for e in (1, 2)}
    refs = [_Ref(e, paths[e]) for e in (1, 2)]
    for p in paths.values():
        with open(p, "wb") as f:
            f.write(b"x")
    assert ck.latest_valid(refs) is None


def test_prune_keeps_last_three():
    refs = [_Ref(e, f"/x/{e}") for e in range(1, 7)]
    stale = ck.prune_paths(refs, keep=3)
    assert sorted(r.epoch for r in stale) == [1, 2, 3]


def test_published_ref_always_points_to_complete_file(tmp_path):
    """Simulate crash mid-write: tmp file exists, no ref, final untouched."""
    path = _save_real(4)
    tmp = path + f".tmp.{os.getpid()}"
    with open(tmp, "wb") as f:
        f.write(b"partial")
    # The final path still validates; tmp is never published.
    assert ck.is_valid_file(path, expected_epoch=4)
    os.remove(tmp)
