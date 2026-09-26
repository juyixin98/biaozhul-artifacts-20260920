"""生成 examples/ 下的请求样例 JSON。

用法: python scripts/generate_examples.py
"""

import json
import math
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from odometry.config import RobotParams
from odometry.synthetic import simulate_samples

OUT_DIR = os.path.join(os.path.dirname(__file__), "..", "examples")

ROBOT = {
    "wheel_diameter_left": 0.16,
    "wheel_diameter_right": 0.16,
    "track_width": 0.4,
    "ticks_per_revolution": 4096,
    "encoder_modulus": 65536,
}


def write(name: str, request: dict) -> None:
    path = os.path.join(OUT_DIR, name)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(request, fh, ensure_ascii=False, indent=2)
        fh.write("\n")
    print(f"written: {path}")


def main() -> None:
    os.makedirs(OUT_DIR, exist_ok=True)
    params = RobotParams.from_dict(ROBOT)
    dt = 0.05

    # 1. 直行: 0.5 m/s × 2 s = 1.0 m
    write(
        "straight.json",
        {
            "robot": ROBOT,
            "samples": simulate_samples(params, [(0.5, 0.0, 2.0)], dt),
        },
    )

    # 2. 原地旋转: 1.0 rad/s × π s = 旋转 π 弧度
    write(
        "rotate_in_place.json",
        {
            "robot": ROBOT,
            "samples": simulate_samples(params, [(0.0, 1.0, math.pi)], dt),
        },
    )

    # 3. 圆弧: v=0.5, omega=0.5 → R=1 m, 转 π/2（四分之一圆）
    write(
        "arc.json",
        {
            "robot": ROBOT,
            "samples": simulate_samples(params, [(0.5, 0.5, math.pi)], dt),
        },
    )

    # 4. 回绕 + 倒车 + 丢样: 起点贴近模值上限，前进触发回绕，再倒车，
    #    中途人为拉大一个时间间隔模拟丢样
    samples = simulate_samples(
        params,
        [(0.5, 0.0, 1.0), (-0.3, 0.0, 1.0)],
        dt,
        initial_left=65500,
        initial_right=65500,
    )
    gap_index = len(samples) // 3
    for i in range(gap_index, len(samples)):
        samples[i]["t"] = round(samples[i]["t"] + 1.0, 6)
    write(
        "wrap_reverse_dropout.json",
        {
            "robot": ROBOT,
            "diagnostics": {
                "max_linear_velocity": 2.0,
                "max_angular_velocity": 10.0,
                "max_dt": 0.2,
            },
            "samples": samples,
        },
    )


if __name__ == "__main__":
    main()
