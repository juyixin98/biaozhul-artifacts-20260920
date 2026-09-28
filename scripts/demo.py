#!/usr/bin/env python3
"""Local end-to-end demonstration of the mixed-precision trainer.

Run:

    python scripts/demo.py             # add --log-dir logs to also dump JSONL

Uses ONLY local synthetic data.  For a single run id it shows:

1. good -> amplified-overflow -> recovery windows with explicit verdicts;
2. master-weight conservation across the skipped window (checked, not assumed);
3. an fp32 control run over identical inputs (never skips);
4. gradient-accumulation atomicity (one bad micro-batch drops the window);
5. checkpoint save/restore including scaler state and LR progress.

Every printed verdict states what was checked and why.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))

import numpy as np  # noqa: E402

from amptrain.checkpoint import load_checkpoint, save_checkpoint  # noqa: E402
from amptrain.config import (  # noqa: E402
    AccumulationConfig,
    OptimizerConfig,
    PrecisionConfig,
)
from amptrain.events import VERSION_CONTEXT  # noqa: E402
from amptrain.trainer import STATUS_COMMITTED, STATUS_SKIPPED, MixedPrecisionTrainer  # noqa: E402
from tests.helpers import (  # noqa: E402
    amplified_batches,
    make_config,
    normal_batches,
)

AMP = 1000.0  # finite but large: overflows fp16 at the scaled-loss stage


def heading(text: str) -> None:
    print(f"\n=== {text} ===")


def check(label: str, ok: bool, detail: str = "") -> None:
    mark = "PASS" if ok else "FAIL"
    print(f"  [{mark}] {label}" + (f" -- {detail}" if detail else ""))
    if not ok:
        raise SystemExit(f"demo assertion failed: {label}")


def snapshot(trainer):
    return {n: trainer.master.matrices[n].copy() for n in ("W1", "W2")}


def identical(a, b) -> bool:
    return all(np.array_equal(a[n], b[n]) for n in ("W1", "W2"))


def print_outcome(o) -> None:
    if o.status == STATUS_COMMITTED:
        print(
            f"  window {o.window_index}: COMMITTED "
            f"(step {o.committed_steps}, scale {o.scale_before}->{o.scale_after}, "
            f"lr {o.lr:g}, fp32 loss {o.mean_loss_fp32:.6f})"
        )
    else:
        print(
            f"  window {o.window_index}: SKIPPED "
            f"(stage={o.overflow_stage}, micro={o.overflow_micro_index}, "
            f"tensors={list(o.nonfinite_tensors)}, scale {o.scale_before}->{o.scale_after})"
        )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--log-dir", default=None, help="optional dir for JSONL event logs")
    args = parser.parse_args()

    print("amptrain local mixed-precision demo")
    print("versions:", json.dumps(VERSION_CONTEXT))
    log_sink = Path(args.log_dir) / "fp16-demo.jsonl" if args.log_dir else None

    # ---- 1. fp16: good, amplified overflow, recovery --------------------
    heading("fp16 run: 2 good windows, 1 amplified overflow, 2 recovery windows")
    cfg16 = make_config()
    trainer = MixedPrecisionTrainer(cfg16, run_id="demo-fp16", log_sink=log_sink)
    print(f"  run_id={trainer.run_id} lowp=float16 init_scale={cfg16.scaler.init_scale}")

    good1 = normal_batches(cfg16, 1, seed=1)[0]
    good2 = normal_batches(cfg16, 1, seed=2)[0]
    bad = amplified_batches(cfg16, AMP, 1, seed=3)[0]
    rec1 = normal_batches(cfg16, 1, seed=4)[0]
    rec2 = normal_batches(cfg16, 1, seed=5)[0]

    for batch in (good1, good2):
        print_outcome(trainer.process_window([batch]))
    pre_skip = snapshot(trainer)  # master weights right before the overflow
    skip = trainer.process_window([bad])
    print_outcome(skip)
    post_skip = snapshot(trainer)
    for batch in (rec1, rec2):
        print_outcome(trainer.process_window([batch]))

    check("amplified window was skipped", skip.status == STATUS_SKIPPED)
    check("overflow located at scaled_loss stage", skip.overflow_stage == "scaled_loss")
    check("scale backed off 128 -> 64", skip.scale_before == 128.0 and skip.scale_after == 64.0)
    check("master weights bit-identical across skip", identical(pre_skip, post_skip))
    check("committed_steps excludes the skip", trainer.counters.committed_steps == 4)
    check("skipped_windows == 1", trainer.counters.skipped_windows == 1)

    # ---- 2. fp32 control: classic dynamic-scaling scenario --------------
    heading("control: healthy inputs with an over-large scale (32768)")
    # Same healthy data for both precisions; only the dtype differs.
    from amptrain.graph import loss_fp32
    from amptrain.config import ScalerConfig
    pool = normal_batches(cfg16, n=8, seed=9)
    big = ScalerConfig(init_scale=32768.0, growth_interval=10**9)
    cfg32b = make_config(precision=PrecisionConfig(lowp_dtype="float32"), scaler=big)
    cfg16b = make_config(precision=PrecisionConfig(lowp_dtype="float16"), scaler=big)
    c32 = MixedPrecisionTrainer(cfg32b, run_id="demo-ctrl-fp32")
    c16 = MixedPrecisionTrainer(cfg16b, run_id="demo-ctrl-fp16")

    def pool_loss(t, c):
        return float(__import__("numpy").mean(
            [loss_fp32(t.master.matrices, x, y, c.model.activation) for x, y in pool]
        ))

    l0_32, l0_16 = pool_loss(c32, cfg32b), pool_loss(c16, cfg16b)
    for _ in range(2):
        for batch in pool:
            c32.process_window([batch])
            print_outcome(c16.process_window([batch]))
    check(
        "fp32 run uninterrupted: 16/16 commits, scale unchanged",
        c32.counters.committed_steps == 16 and c32.counters.skipped_windows == 0
        and c32.scaler.scale == 32768.0,
    )
    check(
        "fp32 learned (loss decreased)",
        pool_loss(c32, cfg32b) < l0_32 * 0.5,
        f"{l0_32:.3f} -> {pool_loss(c32, cfg32b):.3f}",
    )
    check(
        "fp16 skipped on range, backed off, then recovered",
        c16.counters.skipped_windows >= 1 and c16.scaler.scale < 32768.0
        and c16.counters.committed_steps >= 1,
        f"scale 32768 -> {c16.scaler.scale:g}, {c16.counters.skipped_windows} skips",
    )
    check(
        "fp16 still learned overall despite skips",
        pool_loss(c16, cfg16b) < l0_16 * 0.5,
        f"{l0_16:.3f} -> {pool_loss(c16, cfg16b):.3f}",
    )

    # ---- 3. accumulation atomicity --------------------------------------
    heading("gradient accumulation: 3 micro-batches, the middle one overflows")
    cfg_acc = make_config(accumulation=AccumulationConfig(micro_batches=3))
    acc_trainer = MixedPrecisionTrainer(cfg_acc, run_id="demo-accum")
    before = snapshot(acc_trainer)
    window = [
        normal_batches(cfg_acc, 1, seed=11)[0],
        amplified_batches(cfg_acc, AMP, 1, seed=12)[0],
        normal_batches(cfg_acc, 1, seed=13)[0],
    ]
    o = acc_trainer.process_window(window)
    print_outcome(o)
    check("overflowing micro-batch identified at index 1", o.overflow_micro_index == 1)
    check("no partial commit: master weights unchanged", identical(before, snapshot(acc_trainer)))
    check("no optimizer step ran", acc_trainer.counters.committed_steps == 0)
    check("scale backed off despite 2/3 good micro-batches", acc_trainer.scaler.scale == 64.0)

    # ---- 4. checkpoint preserves scaler state and LR progress -----------
    heading("checkpoint save/restore with a step LR schedule")
    cfg_lr = make_config(
        optimizer=OptimizerConfig(lr=0.1, momentum=0.0, schedule="step", step_size=2, gamma=0.5)
    )
    lr_trainer = MixedPrecisionTrainer(cfg_lr, run_id="demo-ckpt")
    lr_trainer.process_window(normal_batches(cfg_lr, 1, seed=21))
    lr_trainer.process_window(normal_batches(cfg_lr, 1, seed=22))  # 2 commits -> next lr 0.05
    skipped = lr_trainer.process_window(amplified_batches(cfg_lr, AMP, 1, seed=23))
    assert skipped.status == STATUS_SKIPPED
    ckpt_dir = ROOT / ".demo_checkpoint"
    path = save_checkpoint(lr_trainer, ckpt_dir)
    print(f"  saved checkpoint to {path} (scale now {lr_trainer.scaler.scale:g})")
    restored = load_checkpoint(path)
    check("scale persisted across checkpoint", restored.scaler.scale == 64.0)
    check("LR progress persisted (2 committed steps)", restored.scheduler.committed_steps == 2)
    o = restored.process_window(normal_batches(cfg_lr, 1, seed=24))
    print_outcome(o)
    check("resumed step is #3 and uses lr 0.05", o.committed_steps == 3 and abs(o.lr - 0.05) < 1e-12)

    # ---- event log excerpt ----------------------------------------------
    heading("event log excerpt (run-correlated, with versions + verdict basis)")
    for event in trainer.log.to_list()[:4]:
        slim = {k: event[k] for k in ("seq", "run_id", "window_index", "event", "detail", "version")}
        print(" ", json.dumps(slim, default=str)[:240])

    print("\nDEMO OK")


if __name__ == "__main__":
    main()
