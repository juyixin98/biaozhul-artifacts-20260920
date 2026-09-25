"""生成完整验收轨迹的 JSON 请求，并通过 process_request 跑出响应。

用法::

    python3 examples/generate_acceptance_demo.py
    # 生成 examples/request_acceptance.json 与 examples/response_acceptance.json

场景与 tests/test_acceptance.py 相同：二维恒速轨迹（x 米 / y 毫米不同量纲）、
连续 15 步全缺测、随后 15 步仅缺 y 轴、固定随机种子。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from kfmu.jsonio import process_request  # noqa: E402

DT = 1.0
T = 160
GAP_START, GAP_END = 60, 75
VEL = (2.0, -1.5)
SIGMA_X, SIGMA_Y_MM = 1.0, 400.0
Q_MAG = 0.005


def main() -> None:
    out_dir = Path(__file__).resolve().parent

    F = [[1, DT, 0, 0], [0, 1, 0, 0], [0, 0, 1, DT], [0, 0, 0, 1]]
    H = [[1, 0, 0, 0], [0, 0, 1000, 0]]
    qb = np.array([[DT**4 / 4, DT**3 / 2], [DT**3 / 2, DT**2]])
    Q = (Q_MAG * np.block([
        [qb, np.zeros((2, 2))],
        [np.zeros((2, 2)), qb],
    ])).tolist()
    R = [[SIGMA_X**2, 0.0], [0.0, SIGMA_Y_MM**2]]

    rng = np.random.default_rng(20260923)
    measurements = []
    for k in range(T):
        tx, ty = VEL[0] * k, VEL[1] * k
        zx = tx + rng.normal(0, SIGMA_X)
        zy = ty * 1000.0 + rng.normal(0, SIGMA_Y_MM)
        if GAP_START <= k < GAP_END:
            measurements.append(None)
        elif GAP_END <= k < GAP_END + 15:
            measurements.append([float(zx), None])
        else:
            measurements.append([float(zx), float(zy)])

    request = {
        "model": {"F": F, "H": H, "Q": Q, "R": R},
        "initial_state": {"x": [0.0] * 4, "P": (np.eye(4) * 100.0).tolist()},
        "measurements": measurements,
        "options": {"return_covariance": False, "diagnostics": True},
    }
    (out_dir / "request_acceptance.json").write_text(
        json.dumps(request, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    response = process_request(request)
    (out_dir / "response_acceptance.json").write_text(
        json.dumps(response, ensure_ascii=False, indent=2, allow_nan=False),
        encoding="utf-8",
    )

    s = response["summary"]
    print(json.dumps(s, ensure_ascii=False, indent=2))
    print("final x =", [round(v, 4) for v in response["final"]["x"]])
    print(f"truth   = [{VEL[0]*(T-1)}, {VEL[0]}, {VEL[1]*(T-1)}, {VEL[1]}]")


if __name__ == "__main__":
    main()
