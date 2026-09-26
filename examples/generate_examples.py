"""Generate the example JSON requests (and their ground truth) deterministically.

Run from the repository root:

    python3 examples/generate_examples.py
"""

import json
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from icp2d.synthetic import make_scan_pair, rectangle_room_scan, straight_wall_scan

HERE = Path(__file__).resolve().parent

SCENARIOS = {
    "01_known_transform": dict(
        world=rectangle_room_scan(spacing=0.05),
        ground_truth=(0.8, -0.5, 0.15),
        noise_sigma=0.01,
        outlier_count=0,
        overlap_ratio=1.0,
        seed=5,
        initial_guess=None,
        description="rectangle room, known transform, light sensor noise",
    ),
    "02_outliers_partial_overlap": dict(
        world=rectangle_room_scan(spacing=0.05),
        ground_truth=(0.8, -0.5, 0.15),
        noise_sigma=0.01,
        outlier_count=180,
        overlap_ratio=0.7,
        seed=2,
        initial_guess=None,
        description="rectangle room with 180 outliers and 70% overlap",
    ),
    "03_degenerate_wall": dict(
        world=straight_wall_scan(length=20.0, spacing=0.05),
        ground_truth=(1.0, 0.3, 0.0),
        noise_sigma=0.005,
        outlier_count=0,
        overlap_ratio=1.0,
        seed=3,
        initial_guess=None,
        description="single straight wall: along-wall translation unobservable",
    ),
    "04_local_optimum_square": dict(
        world=rectangle_room_scan(width=10.0, height=10.0, spacing=0.05),
        ground_truth=(0.0, 0.0, 0.0),
        noise_sigma=0.005,
        outlier_count=0,
        overlap_ratio=1.0,
        seed=4,
        initial_guess=(0.0, 0.0, 1.5707963),
        description="square room with a quarter-turn initial guess (symmetry trap)",
    ),
    "05_bad_initial_guess": dict(
        world=rectangle_room_scan(spacing=0.05),
        ground_truth=(0.8, -0.5, 0.15),
        noise_sigma=0.0,
        outlier_count=0,
        overlap_ratio=1.0,
        seed=1,
        initial_guess=(10.0, 10.0, 0.0),
        description="initial guess 10 m off: correspondence gate rejects everything",
    ),
}


def main() -> None:
    for name, spec in SCENARIOS.items():
        scenario = make_scan_pair(
            spec["world"],
            np.array(spec["ground_truth"]),
            noise_sigma=spec["noise_sigma"],
            outlier_count=spec["outlier_count"],
            overlap_ratio=spec["overlap_ratio"],
            seed=spec["seed"],
            description=spec["description"],
        )
        request = {
            "source": scenario.source.tolist(),
            "target": scenario.target.tolist(),
        }
        if spec["initial_guess"] is not None:
            request["initial_guess"] = list(spec["initial_guess"])
        (HERE / f"{name}.json").write_text(json.dumps(request))
        ground_truth = {
            "description": spec["description"],
            "ground_truth_pose": list(spec["ground_truth"]),
            "noise_sigma": spec["noise_sigma"],
            "outlier_count": spec["outlier_count"],
            "overlap_ratio": spec["overlap_ratio"],
        }
        (HERE / f"{name}.ground_truth.json").write_text(json.dumps(ground_truth, indent=2))
        print(f"wrote {name}.json ({len(scenario.source)} source, "
              f"{len(scenario.target)} target points)")


if __name__ == "__main__":
    main()
