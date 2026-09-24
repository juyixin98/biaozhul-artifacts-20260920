"""Acceptance/benchmark CLI: run every scene and print precision & recall.

Usage::

    python scripts/evaluate_scenes.py            # all scenes, table
    python scripts/evaluate_scenes.py slope      # one scene
"""

from __future__ import annotations

import os
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.segmentation.chunking import ChunkConfig, segment_cloud  # noqa: E402
from app.segmentation.metrics import evaluate_report  # noqa: E402
from app.segmentation.ransac import RansacConfig  # noqa: E402
from app.segmentation.synth import SCENES, UNKNOWN  # noqa: E402

# Scenes whose constructed truth is deliberately "no answer exists":
# predictions must be `unknown` there, and scoring reflects that.
ABSTENTION_SCENES = {"sparse", "steep_slope", "noise_dominated"}


def run_scene(name: str, ransac_cfg: RansacConfig | None = None,
              chunk_cfg: ChunkConfig | None = None) -> dict:
    scene = SCENES[name]()
    out = segment_cloud(np.asarray(scene.points, dtype=np.float64),
                        ransac_cfg, chunk_cfg)
    report = evaluate_report(out.labels, scene.labels)
    predicted_unknown = [
        i for i, lab in enumerate(out.labels) if lab == UNKNOWN
    ]
    truth_unknown = {i for i, lab in enumerate(scene.labels) if lab == UNKNOWN}
    # "Abstention coverage": of the points for which no ground truth can be
    # claimed (unknowable scenes), how many were also returned as unknown.
    correctly_abstained = sum(1 for i in predicted_unknown if i in truth_unknown)
    # For abstention scenes every point is unknowable: a definite label on any
    # of them is an overconfident mistake.
    falsely_decided = sum(
        1 for i, lab in enumerate(out.labels)
        if i in truth_unknown and lab != UNKNOWN
    )
    return {
        "scene": scene,
        "output": out,
        "report": report,
        "abstention_correct": correctly_abstained,
        "abstention_total": len(truth_unknown),
        "falsely_decided": falsely_decided,
    }


def print_row(name: str, res: dict) -> None:
    r = res["report"]
    g = r.ground
    print(
        f"{name:<18} N={r.total_points:<5} "
        f"P={g.precision:.3f} R={g.recall:.3f} F1={g.f1:.3f} "
        f"acc={r.accuracy:.3f} unk_pred={r.unknown_predictions:<4} "
        f"abstain_ok={res['abstention_correct']}/{res['abstention_total']} "
        f"overconfident={res['falsely_decided']}"
    )


def main(argv: list[str]) -> int:
    names = [argv[1]] if len(argv) > 1 else list(SCENES)
    missing = [n for n in names if n not in SCENES]
    if missing:
        print(f"unknown scene(s): {missing}", file=sys.stderr)
        return 2
    print(f"{'scene':<18} {'':6} ground-class metrics vs constructed truth")
    print("-" * 88)
    failures = []
    for name in names:
        res = run_scene(name)
        print_row(name, res)
        r = res["report"].ground
        if name in ABSTENTION_SCENES:
            # Every constructed-unknown point must be abstained on.
            if res["falsely_decided"] != 0:
                failures.append((name, f"{res['falsely_decided']} overconfident labels on unknowable points"))
        else:
            if r.precision < 0.90 or r.recall < 0.90:
                failures.append((name, f"P={r.precision:.3f} R={r.recall:.3f}"))
    print("-" * 88)
    if failures:
        print("ACCEPTANCE FAILED:")
        for name, why in failures:
            print(f"  - {name}: {why}")
        return 1
    print("ACCEPTANCE PASSED: P>=0.90 and R>=0.90 on decidable scenes; "
          "full abstention on unknowable scenes.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
