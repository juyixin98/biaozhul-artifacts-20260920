"""Acceptance comparison: interrupted/resumed runs vs an uninterrupted run.

Runs the same deterministic configuration:

* once straight through (reference)
* once for each interruption schedule in ``interrupt_steps_list``:
  fresh -> run -> simulate process exit -> rebuild from disk -> ... -> finish

Two kinds of interruption are supported by the same mechanism:

* interruption landing on a commit boundary (``checkpoint_every``) resumes
  exactly at that step;
* interruption landing *between* commits loses the uncommitted steps and
  replays them after resume. Recovery is still exact because the checkpoint
  stores optimizer state, RNG state and the data cursor: the resumed run
  draws the identical batch for every replayed step, so the whole update
  sequence matches the uninterrupted run step-for-step.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any

import numpy as np

from .checkpoint import save_checkpoint
from .config import LOSS_ATOL, PARAM_ATOL, TrainConfig
from .trainer import Trainer


@dataclass(frozen=True)
class RunOutcome:
    label: str
    interrupt_steps: tuple[int, ...]
    global_step: int
    W: np.ndarray
    b: float
    eval_loss: float
    loss_history: list[dict[str, Any]]


@dataclass(frozen=True)
class ComparisonReport:
    config: dict[str, Any]
    reference: RunOutcome
    interrupted: list[RunOutcome]
    param_atol: float
    loss_atol: float
    results: list[dict[str, Any]] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return all(r["passed"] for r in self.results)


def run_uninterrupted(cfg: TrainConfig) -> RunOutcome:
    trainer = Trainer.fresh(cfg)
    trainer.train(run_dir=None)
    snap = trainer.snapshot()
    return RunOutcome(
        label="uninterrupted",
        interrupt_steps=(),
        global_step=snap["global_step"],
        W=snap["W"],
        b=snap["b"],
        eval_loss=snap["loss"],
        loss_history=trainer.loss_history,
    )


def run_with_interruptions(
    cfg: TrainConfig, interrupt_steps: list[int], run_dir: str
) -> RunOutcome:
    """Run training with simulated process exits at the given global steps.

    After running toward each target the in-memory trainer is discarded and
    rebuilt from the committed checkpoint on disk — exactly what a restarted
    service process does. The resume point may precede the target when the
    target falls between two checkpoint commits; the intervening steps are
    deterministically replayed.
    """
    os.makedirs(run_dir, exist_ok=True)
    if not interrupt_steps:
        raise ValueError("interrupt_steps must be non-empty")
    if any(s <= 0 or s >= cfg.total_steps for s in interrupt_steps):
        raise ValueError(
            f"interrupt steps must lie within (0, {cfg.total_steps})"
        )
    if list(interrupt_steps) != sorted(interrupt_steps) or len(set(interrupt_steps)) != len(
        interrupt_steps
    ):
        raise ValueError("interrupt_steps must be strictly increasing")

    label = "interrupt@" + ",".join(str(s) for s in interrupt_steps)

    trainer = Trainer.fresh(cfg)
    save_checkpoint(run_dir, trainer.build_state())  # step-0 commit

    for target in interrupt_steps:
        # Fresh process: rebuild everything from the last committed checkpoint.
        trainer = Trainer.resume(run_dir)
        committed_step = trainer.cursor.global_step
        trainer.train(run_dir=run_dir, stop_after=target - committed_step)
        # Simulated crash at/around `target`: drop the trainer without saving
        # anything beyond the periodic commits already performed by train().

    # Final restart: resume and run until training completes.
    trainer = Trainer.resume(run_dir)
    trainer.train(run_dir=run_dir)

    snap = trainer.snapshot()
    return RunOutcome(
        label=label,
        interrupt_steps=tuple(interrupt_steps),
        global_step=snap["global_step"],
        W=snap["W"],
        b=snap["b"],
        eval_loss=snap["loss"],
        loss_history=trainer.loss_history,
    )


def compare(
    cfg: TrainConfig,
    interrupt_steps_list: list[list[int]],
    runs_root: str,
    param_atol: float = PARAM_ATOL,
    loss_atol: float = LOSS_ATOL,
) -> ComparisonReport:
    reference = run_uninterrupted(cfg)

    interrupted: list[RunOutcome] = []
    results: list[dict[str, Any]] = []
    for i, steps in enumerate(interrupt_steps_list):
        run_dir = os.path.join(runs_root, f"interrupt_{i}")
        outcome = run_with_interruptions(cfg, steps, run_dir)
        interrupted.append(outcome)

        param_diff = float(np.max(np.abs(outcome.W - reference.W)))
        b_diff = abs(outcome.b - reference.b)
        loss_diff = abs(outcome.eval_loss - reference.eval_loss)

        ref_losses = [item["loss"] for item in reference.loss_history]
        got_losses = [item["loss"] for item in outcome.loss_history]
        history_len_match = len(ref_losses) == len(got_losses)
        history_max_diff = (
            max(abs(a - b) for a, b in zip(ref_losses, got_losses))
            if history_len_match
            else float("inf")
        )

        passed = (
            param_diff <= param_atol
            and b_diff <= param_atol
            and loss_diff <= loss_atol
            and history_len_match
            and history_max_diff <= loss_atol
        )
        results.append(
            {
                "label": outcome.label,
                "interrupt_steps": list(outcome.interrupt_steps),
                "passed": passed,
                "W_max_abs_diff": param_diff,
                "b_abs_diff": b_diff,
                "eval_loss_ref": reference.eval_loss,
                "eval_loss": outcome.eval_loss,
                "eval_loss_abs_diff": loss_diff,
                "history_len_ref": len(ref_losses),
                "history_len": len(got_losses),
                "history_len_match": history_len_match,
                "history_max_abs_diff": history_max_diff,
            }
        )

    return ComparisonReport(
        config=cfg.to_dict(),
        reference=reference,
        interrupted=interrupted,
        param_atol=param_atol,
        loss_atol=loss_atol,
        results=results,
    )
