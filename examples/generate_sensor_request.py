"""生成合成传感器采样请求样例（examples/request_sensor_fit.json）。

用固定随机种子在真实线性轨迹上叠加高斯噪声，模拟传感器观测；
库内通过最小二乘拟合还原轨迹后做解析碰撞分析。纯合成数据，
不读取任何硬件。
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np

OUT_PATH = Path(__file__).resolve().parent / "request_sensor_fit.json"

# 真实运动参数（仅用于生成合成观测，请求中不出现）
ROBOT = {"center": [0.0, 0.0], "velocity": [1.0, 0.0], "radius": 1.0}
OBSTACLE = {"center": [6.0, 0.5], "velocity": [-1.0, 0.0], "radius": 1.0}
NOISE_STD = 0.02
N_SAMPLES = 21
SEED = 42


def _sample(body: dict, rng: np.random.Generator) -> list:
    t = np.linspace(0.0, 2.0, N_SAMPLES)
    center = np.asarray(body["center"])
    velocity = np.asarray(body["velocity"])
    xy = center + np.outer(t, velocity) + rng.normal(0.0, NOISE_STD, (len(t), 2))
    return [[round(float(ti), 6), round(float(x), 6), round(float(y), 6)]
            for ti, (x, y) in zip(t, xy)]


def main() -> None:
    rng = np.random.default_rng(SEED)
    request = {
        "description": (
            "合成传感器采样场景：真实轨迹 robot c=(0,0) v=(1,0)，"
            "obstacle c=(6,0.5) v=(-1,0)，叠加 N(0, 0.02) 观测噪声；"
            "真值最早接触 t≈2.032（手算：d0=(-6,-0.5), v_rel=(2,0), R=2 => "
            "a=4,b=-24,c=32.25,D=60 => t=(24±√60)/8）"
        ),
        "time_window": [0.0, 4.0],
        "robot": {"radius": ROBOT["radius"], "samples": _sample(ROBOT, rng)},
        "obstacles": [
            {"radius": OBSTACLE["radius"], "samples": _sample(OBSTACLE, rng)}
        ],
    }
    OUT_PATH.write_text(
        json.dumps(request, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(f"已生成 {OUT_PATH}")


if __name__ == "__main__":
    main()
