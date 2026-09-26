"""Run every example request through the CLI and score it against ground truth.

Run from the repository root:

    python3 examples/run_demo.py
"""

import json
import subprocess
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from icp2d.geometry import pose_error

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent


def main() -> int:
    request_paths = sorted(p for p in HERE.glob("*.json") if "ground_truth" not in p.name)
    print(f"{'example':34s} {'status':22s} {'uncertain':9s} {'rmse':>8s} "
          f"{'err_x':>7s} {'err_y':>7s} {'err_th':>7s}")
    for request_path in request_paths:
        name = request_path.stem
        truth = json.loads((HERE / f"{name}.ground_truth.json").read_text())
        proc = subprocess.run(
            [sys.executable, "-m", "icp2d", str(request_path), "--compact"],
            capture_output=True, text=True, cwd=ROOT,
        )
        response = json.loads(proc.stdout)
        if not response.get("success"):
            print(f"{name:34s} {response.get('status', 'error'):22s} "
                  f"{str(response.get('uncertain')):9s} {'-':>8s} "
                  f"{'-':>7s} {'-':>7s} {'-':>7s}")
            continue
        pose = np.array(
            [response["pose"]["x"], response["pose"]["y"], response["pose"]["theta"]]
        )
        error = pose_error(pose, np.array(truth["ground_truth_pose"]))
        print(f"{name:34s} {response['status']:22s} "
              f"{str(response['uncertain']):9s} {response['final_rmse']:8.4f} "
              f"{error[0]:7.4f} {error[1]:7.4f} {error[2]:7.4f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
