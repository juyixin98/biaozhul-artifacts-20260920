#!/usr/bin/env python3
"""Local demo: overflow, skip semantics, checkpoint/resume, fp32 comparison.

Usage:
    PYTHONPATH=src python scripts/demo.py

The demo is fully synthetic and deterministic. It prints one line per
micro-step with the run identity, decision, and scale, then a summary of
the conservation and resume checks.
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from mptrainer.checkpoint import load_checkpoint, save_checkpoint
from mptrainer.config import TrainerConfig
from mptrainer.data import SyntheticBatchSource
from mptrainer.runlog import runtime_versions
from mptrainer.trainer import MixedPrecisionTrainer
from mptrainer.validation import max_abs_diff, params_bitwise_equal, snapshot

OVERFLOW_AMPLIFY = 1.0e4  # pushes fp16 activations/grads past 65504 -> inf


def print_step(rec) -> None:
    loss = "overflow" if rec.loss is None else f"{rec.loss:.6g}"
    print(
        f"  [{rec.run_id}] micro={rec.micro_step:3d} opt={rec.optimizer_step:3d} "
        f"{rec.decision:<16s} scale={rec.scale_before:8.1f}->{rec.scale_after:8.1f} "
        f"lr={rec.lr:.6g} loss={loss}"
    )


def main() -> None:
    versions = runtime_versions()
    print(f"versions: {versions}")

    config = TrainerConfig(
        layer_sizes=[8, 16, 1], seed=1234, base_lr=0.01, lr_decay=0.05,
        momentum=0.9, accum_steps=2, low_dtype="float16",
        init_scale=1024.0, growth_interval=4,
    ).validate()
    source = SyntheticBatchSource(config.layer_sizes[0], config.layer_sizes[-1])

    run_id = "demo-fp16"
    trainer = MixedPrecisionTrainer(config, run_id=run_id, log_dir="logs")
    print(f"\n== phase 1: clean windows (run {run_id}) ==")
    for _ in range(4):
        print_step(trainer.train_step(*source.batch(16)))

    print("\n== phase 2: amplified input triggers overflow ==")
    before = snapshot(trainer.master)
    x, y = source.batch(16, amplify=OVERFLOW_AMPLIFY)
    rec = trainer.train_step(x, y)
    print_step(rec)
    conserved = params_bitwise_equal(before, trainer.master)
    print(f"  master weights conserved during skip: {conserved}")
    assert conserved, "demo invariant violated: master weights changed on overflow"

    print("\n== phase 3: recovery after backoff ==")
    for _ in range(4):
        print_step(trainer.train_step(*source.batch(16)))

    print("\n== phase 4: checkpoint / resume identity ==")
    with tempfile.TemporaryDirectory() as tmp:
        ckpt = save_checkpoint(trainer, Path(tmp) / run_id)
        print(f"  checkpoint written to {ckpt}")
        resumed = load_checkpoint(ckpt, run_id=f"{run_id}-resumed", log_dir="logs")
        print(f"  restored scale={resumed.scaler.scale} "
              f"opt_step={resumed.optimizer_step} micro_step={resumed.micro_step}")
        # Continue both trainers on identical batches; trajectories must match.
        for _ in range(4):
            x, y = source.batch(16)
            r_a = trainer.train_step(x, y)
            r_b = resumed.train_step(x, y)
        print_step(r_a)
        print_step(r_b)
        diff = max_abs_diff(trainer.master, resumed.master)
        print(f"  max |master - resumed_master| after 4 more steps: {diff:.3g}")
        assert diff == 0.0, "resumed run diverged from uninterrupted run"

    print("\n== phase 5: fp16 vs fp32 reference (same seed, same batches) ==")
    ref_config = TrainerConfig(**{**config.to_dict(), "low_dtype": "float32"}).validate()
    ref_source = SyntheticBatchSource(config.layer_sizes[0], config.layer_sizes[-1])
    ref = MixedPrecisionTrainer(ref_config, run_id="demo-fp32-ref", log_dir="logs")
    fp16 = MixedPrecisionTrainer(config, run_id="demo-fp16-clean", log_dir="logs")
    clean_source = SyntheticBatchSource(config.layer_sizes[0], config.layer_sizes[-1])
    for _ in range(10):
        x, y = clean_source.batch(16)
        fp16.train_step(x, y)
        ref.train_step(x, y)
    diff = max_abs_diff(fp16.master, ref.master)
    scale = max(abs(v) for p in ref.master.values() for v in p.ravel())
    print(f"  max |fp16_master - fp32_master| = {diff:.3g} "
          f"(relative to max weight {scale:.3g}: {diff / scale:.2%})")
    print("\ndemo finished: all invariants held")


if __name__ == "__main__":
    main()
