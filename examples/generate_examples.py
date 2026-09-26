#!/usr/bin/env python3
"""生成 examples/ 下的合成请求样例（纯合成数据，不依赖硬件）。

运行: python3 examples/generate_examples.py
"""

from __future__ import annotations

import json
import math
import os

ROBOT = {
    "wheel_diameter_m": 0.1,
    "track_width_m": 0.5,
    "ticks_per_rev": 1000,
    "encoder_min": 0,
    "encoder_max": 65535,
    "max_linear_velocity_mps": 3.0,
    "max_angular_velocity_rps": 12.0,
    "max_sample_period_s": 0.5,
}

# 每 tick 对应的轮面位移（米）
MPT = math.pi * ROBOT["wheel_diameter_m"] / ROBOT["ticks_per_rev"]


def make_samples(per_step: list[tuple[int, int]], dt: float, start=(0, 0),
                 modulus: int = 65536):
    """由每步 (左增量, 右增量) 生成累计计数采样序列（按模回绕）。"""
    left, right = start
    samples = [{"t": 0.0, "left": left, "right": right}]
    t = 0.0
    for dl, dr in per_step:
        t += dt
        left = (left + dl) % modulus
        right = (right + dr) % modulus
        samples.append({"t": round(t, 6), "left": left, "right": right})
    return samples


def write(name: str, request: dict) -> None:
    path = os.path.join(os.path.dirname(__file__), name)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(request, f, ensure_ascii=False, indent=2)
        f.write("\n")
    print(f"wrote {path}")


def main() -> None:
    # 1. 直行：每步左右各 +1000 tick = 0.1π m，10 步共 π 米
    write(
        "request_straight.json",
        {
            "robot": ROBOT,
            "samples": make_samples([(1000, 1000)] * 10, dt=0.1),
        },
    )

    # 2. 原地旋转：左轮 -200 / 右轮 +200，10 步，总转角 0.8π
    write(
        "request_rotate_in_place.json",
        {
            "robot": ROBOT,
            "samples": make_samples([(-200, 200)] * 10, dt=0.1),
        },
    )

    # 3. 圆弧：每步左 750 / 右 1250 tick -> R=1m，每步转角 0.1π，
    #    5 步转 90°，理论终点 (1, 1, π/2)
    write(
        "request_arc.json",
        {
            "robot": ROBOT,
            "samples": make_samples([(750, 1250)] * 5, dt=0.5),
        },
    )

    # 4. 回绕 + 倒车：小量程计数器 0..1023，正向越过上限后反向越过下限
    robot_small = dict(ROBOT, encoder_max=1023)
    steps = [(50, 50)] * 6 + [(-80, -80)] * 4
    left, right = 1000, 1000
    samples = [{"t": 0.0, "left": left, "right": right}]
    t = 0.0
    for dl, dr in steps:
        t += 0.1
        left = (left + dl) % 1024
        right = (right + dr) % 1024
        samples.append({"t": round(t, 6), "left": left, "right": right})
    write(
        "request_wraparound_reverse.json",
        {"robot": robot_small, "samples": samples},
    )

    # 5. 丢样 + 速度跳变：中间一个 1.2s 大间隔，随后一步位移过大
    steps = [(100, 100)] * 3 + [(20000, 20000)] + [(100, 100)] * 2
    samples = [{"t": 0.0, "left": 0, "right": 0}]
    t = 0.0
    left = right = 0
    for i, (dl, dr) in enumerate(steps):
        t += 1.2 if i == 3 else 0.1  # 第 4 步前制造丢样间隔
        left += dl
        right += dr
        samples.append({"t": round(t, 6), "left": left, "right": right})
    write(
        "request_gap_and_jump.json",
        {"robot": ROBOT, "samples": samples},
    )


if __name__ == "__main__":
    main()
