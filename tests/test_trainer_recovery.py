"""Integration tests: interrupted+resumed training must match uninterrupted."""

from __future__ import annotations

import os

import numpy as np
import pytest

from checkpoint_service.checkpoint import save_checkpoint
from checkpoint_service.config import LOSS_ATOL, PARAM_ATOL, TrainConfig
from checkpoint_service.compare import compare
from checkpoint_service.errors import DatasetMismatchError
from checkpoint_service.synthetic import make_dataset
from checkpoint_service.trainer import Trainer


def _cfg(**overrides) -> TrainConfig:
    return TrainConfig(
        n_samples=96,
        n_features=4,
        batch_size=16,
        n_epochs=3,
        checkpoint_every=5,
        **overrides,
    )


def _batch_sequence(cfg: TrainConfig) -> list[tuple[int, ...]]:
    """The exact sequence of mini-batch index tuples for a clean run."""
    trainer = Trainer.fresh(cfg)
    batches: list[tuple[int, ...]] = []

    def capture(step: int, committed: bool) -> None:
        # The batch for this step is cursor permutation slice at current pos.
        end = trainer.cursor.pos
        start = end - cfg.batch_size
        batches.append(tuple(int(i) for i in trainer.cursor.permutation[start:end]))

    trainer.train(on_step=capture)
    return batches


@pytest.mark.integration
def test_replayed_batches_match_uninterrupted_sequence(tmp_path: str) -> None:
    """A mid-interval crash must deterministically replay the exact batches."""
    cfg = _cfg()  # checkpoint_every=5
    expected = _batch_sequence(cfg)  # expected[step-1] = batch index tuple

    run_dir = os.path.join(tmp_path, "run")
    trainer = Trainer.fresh(cfg)
    save_checkpoint(run_dir, trainer.build_state())

    # Run to step 7 then discard the in-memory trainer (simulated exit).
    trainer.train(run_dir=run_dir, stop_after=7)
    assert Trainer.resume(run_dir).cursor.global_step == 5  # last commit

    replayed: list[tuple[int, ...]] = []

    def capture(step: int, committed: bool) -> None:
        end = trainer.cursor.pos
        start = end - cfg.batch_size
        replayed.append(tuple(int(i) for i in trainer.cursor.permutation[start:end]))

    # Restart: steps 6 and 7 are replayed, then training continues.
    trainer = Trainer.resume(run_dir)
    trainer.train(run_dir=run_dir, on_step=capture)

    assert trainer.cursor.global_step == cfg.total_steps
    assert len(trainer.loss_history) == cfg.total_steps
    # The first two batches after resume must be identical to the batches the
    # uninterrupted run used at global steps 6 and 7 (deterministic replay).
    assert replayed[:2] == expected[5:7]
    # And the entire post-resume segment matches steps 6..N of the clean run.
    assert replayed == expected[5:]


@pytest.mark.integration
@pytest.mark.parametrize(
    "schedule",
    [
        [1],
        [4],
        [5],      # commit boundary
        [6],      # one past boundary -> replay
        [12],     # epoch boundary for n=96,bs=16 (steps/epoch=6)
        [13],     # just after epoch boundary
        [17],     # final step is 18
        [3, 10],  # two interruptions
    ],
)
def test_interrupted_runs_match_uninterrupted(
    tmp_path: str, schedule: list[int]
) -> None:
    cfg = _cfg()
    report = compare(cfg, [schedule], runs_root=tmp_path)
    assert report.passed, report.results
    r = report.results[0]
    assert r["history_len_match"]
    # Float32 ops are replayed in the same order: expect exact agreement, and
    # in any case never beyond the documented tolerance.
    assert r["W_max_abs_diff"] <= PARAM_ATOL
    assert r["b_abs_diff"] <= PARAM_ATOL
    assert r["eval_loss_abs_diff"] <= LOSS_ATOL
    assert r["history_max_abs_diff"] <= LOSS_ATOL


@pytest.mark.integration
def test_default_config_full_acceptance_schedule(tmp_path: str) -> None:
    cfg = TrainConfig()
    spe = cfg.steps_per_epoch
    schedules = [
        [1],
        [cfg.checkpoint_every - 1],
        [cfg.checkpoint_every],
        [spe],
        [spe + 1],
        [cfg.total_steps - 1],
        [8, 24, spe * 2 + 3],
    ]
    report = compare(cfg, schedules, runs_root=tmp_path)
    assert report.passed, report.results


@pytest.mark.integration
def test_interruption_validation(tmp_path: str) -> None:
    cfg = _cfg()
    from checkpoint_service.compare import run_with_interruptions

    with pytest.raises(ValueError, match="within"):
        run_with_interruptions(cfg, [0], str(tmp_path))
    with pytest.raises(ValueError, match="strictly increasing"):
        run_with_interruptions(cfg, [3, 3], str(tmp_path / "a"))
    with pytest.raises(ValueError, match="non-empty"):
        run_with_interruptions(cfg, [], str(tmp_path / "b"))


@pytest.mark.integration
def test_loss_actually_decreases(tmp_path: str) -> None:
    cfg = TrainConfig(
        n_samples=256, n_features=4, batch_size=16, n_epochs=12, checkpoint_every=8
    )
    trainer = Trainer.fresh(cfg)
    result = trainer.train(run_dir=None)
    first = trainer.loss_history[0]["loss"]
    assert result.final_loss < first
    assert result.final_loss < 0.05  # synthetic linear data is easy to fit


@pytest.mark.integration
def test_resume_refuses_when_dataset_fingerprint_differs(tmp_path: str) -> None:
    cfg = _cfg(data_seed=1)
    run_dir = os.path.join(tmp_path, "run")
    trainer = Trainer.fresh(cfg)
    save_checkpoint(run_dir, trainer.build_state())

    # Tamper with the dataset identity: regenerate under a different seed by
    # rewriting config + fingerprint inside the checkpoint payload.
    import hashlib
    import json
    import pickle

    from checkpoint_service.checkpoint import META_NAME, PAYLOAD_NAME

    other = make_dataset(_cfg(data_seed=2))
    state = trainer.build_state()
    state["data_fingerprint"] = other.fingerprint
    blob = pickle.dumps(state, protocol=pickle.HIGHEST_PROTOCOL)
    with open(os.path.join(run_dir, PAYLOAD_NAME), "wb") as f:
        f.write(blob)
    with open(os.path.join(run_dir, META_NAME), "w") as f:
        json.dump(
            {"sha256": hashlib.sha256(blob).hexdigest(), "size": len(blob), "format_version": 1},
            f,
        )

    with pytest.raises(DatasetMismatchError, match="fingerprint"):
        Trainer.resume(run_dir)
