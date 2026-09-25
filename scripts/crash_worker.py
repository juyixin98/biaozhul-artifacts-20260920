#!/usr/bin/env python3
"""Worker process for the real SIGKILL crash-recovery demo.

Creates the run (if needed), resumes from the committed checkpoint, and
trains. When CRASH_AT_STEP is set, the process replaces itself with a hard
``os._exit`` immediately after that optimizer step — equivalent to SIGKILL
from the trainer's point of view (no Python cleanup, no extra checkpoint):

* at a commit boundary the step's checkpoint is already durable, so resume
  continues exactly there;
* between boundaries the step is lost and deterministically replayed.

Environment
-----------
RUN_DIR          directory for this run (created/committed checkpoint there)
CRASH_AT_STEP    global step after which to hard-exit (optional)
N_EPOCHS etc.    config overrides (optional)
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from checkpoint_service.checkpoint import committed, save_checkpoint
from checkpoint_service.config import TrainConfig
from checkpoint_service.trainer import Trainer


def _cfg() -> TrainConfig:
    return TrainConfig(
        n_samples=int(os.environ.get("N_SAMPLES", "512")),
        n_features=int(os.environ.get("N_FEATURES", "8")),
        data_seed=int(os.environ.get("DATA_SEED", "20260925")),
        train_seed=int(os.environ.get("TRAIN_SEED", "42")),
        batch_size=int(os.environ.get("BATCH_SIZE", "32")),
        lr=float(os.environ.get("LR", "0.05")),
        momentum=float(os.environ.get("MOMENTUM", "0.9")),
        l2=float(os.environ.get("L2", "0.0001")),
        n_epochs=int(os.environ.get("N_EPOCHS", "4")),
        checkpoint_every=int(os.environ.get("CHECKPOINT_EVERY", "8")),
    )


def main() -> int:
    run_dir = os.environ["RUN_DIR"]
    crash_at = os.environ.get("CRASH_AT_STEP")
    crash_at = int(crash_at) if crash_at else None
    cfg = _cfg()
    os.makedirs(run_dir, exist_ok=True)

    if not committed(run_dir):
        trainer = Trainer.fresh(cfg)
        save_checkpoint(run_dir, trainer.build_state())
        print(f"[worker {os.getpid()}] created run at {run_dir}", flush=True)
    else:
        trainer = Trainer.resume(run_dir)
        print(
            f"[worker {os.getpid()}] resumed at step {trainer.cursor.global_step}",
            flush=True,
        )

    def on_step(step: int, committed_this_step: bool) -> None:
        if crash_at is not None and step == crash_at:
            where = "commit-boundary" if committed_this_step else "mid-interval"
            print(
                f"[worker {os.getpid()}] HARD EXIT at step {step} ({where})",
                flush=True,
            )
            # 137 = 128 + SIGKILL(9); os._exit skips all cleanup like SIGKILL.
            os._exit(137)

    result = trainer.train(run_dir=run_dir, on_step=on_step)
    print(
        f"[worker {os.getpid()}] finished at step {result.global_step} "
        f"eval_loss={result.final_loss:.8f}",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
