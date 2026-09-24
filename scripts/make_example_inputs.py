"""生成 examples/ 下的合成输入数据（用于手工验收与自动化测试）。

场景：
1. temp_drift.json   静止采集 + 线性升温 -> 陀螺零偏随温度线性漂移
2. sudden_motion.json 静止-运动-静止（含突然开始/结束的运动）-> 运动不得混入
3. spikes.json       静止数据中插入孤立异常峰值（幅度极大，仅 1~2 个样本宽）
4. duplicate_ts.json 含完全重复时间戳（一致重复可合并）与大时间缺口分段
5. empty_static.json 全程运动 -> 零偏不可观测的负例
"""

from __future__ import annotations

import json
import os

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "..", "examples")
G = 9.80665


def _save(name: str, payload: dict) -> None:
    os.makedirs(OUT, exist_ok=True)
    path = os.path.join(OUT, name)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False)
    print(f"wrote {path} ({len(payload['timestamps'])} samples)")


def _static_imu(t: np.ndarray, bias_g: np.ndarray, rng, noise_g=0.002, noise_a=0.01):
    g = np.tile(bias_g, (len(t), 1)) + rng.normal(0, noise_g, (len(t), 3))
    a = np.zeros((len(t), 3))
    a[:, 2] = G
    a += rng.normal(0, noise_a, (len(t), 3))
    return a, g


def gen_temp_drift() -> None:
    rng = np.random.default_rng(42)
    fs = 100.0
    t = np.arange(0, 120, 1 / fs)
    temp = 20.0 + 0.2 * t  # 20°C -> 44°C
    b0 = np.array([0.01, -0.02, 0.005])
    kT = np.array([3.0e-4, -2.0e-4, 1.0e-4])  # rad/s/°C
    bias_t = b0[None, :] + kT[None, :] * (temp - 20.0)[:, None]
    g = bias_t + rng.normal(0, 0.0015, (len(t), 3))
    a = np.zeros((len(t), 3))
    a[:, 2] = G
    a += rng.normal(0, 0.008, (len(t), 3))
    _save(
        "temp_drift.json",
        {
            "timestamps": t.tolist(),
            "accel": a.tolist(),
            "gyro": g.tolist(),
            "temperature": temp.tolist(),
            "units": {"accel": "ms2", "gyro": "rads", "time": "s"},
        },
    )


def gen_sudden_motion() -> None:
    rng = np.random.default_rng(7)
    fs = 100.0
    t = np.arange(0, 60, 1 / fs)
    b = np.array([0.01, -0.02, 0.005])
    a, g = _static_imu(t, b, rng)
    m = ((t >= 20) & (t < 40))
    # 突然开始、突然结束的大幅运动
    g[m] += rng.normal(0, 0.4, (m.sum(), 3))
    a[m] += rng.normal(0, 2.0, (m.sum(), 3))
    # 尖锐起止沿
    edge = np.isclose(t, 20.0, atol=1 / fs) | np.isclose(t, 40.0, atol=1 / fs)
    g[edge] += np.array([2.0, -2.0, 1.5])
    _save(
        "sudden_motion.json",
        {
            "timestamps": t.tolist(),
            "accel": a.tolist(),
            "gyro": g.tolist(),
            "units": {"accel": "ms2", "gyro": "rads", "time": "s"},
        },
    )


def gen_spikes() -> None:
    rng = np.random.default_rng(99)
    fs = 100.0
    t = np.arange(0, 30, 1 / fs)
    b = np.array([0.008, 0.004, -0.006])
    a, g = _static_imu(t, b, rng)
    # 孤立大毛刺（1~2 个样本宽），幅度远超噪声与零偏
    for i in [314, 1500, 1501, 2450]:
        g[i] += np.array([5.0, -4.0, 6.0])
        a[i] += np.array([30.0, -25.0, 20.0])
    _save(
        "spikes.json",
        {
            "timestamps": t.tolist(),
            "accel": a.tolist(),
            "gyro": g.tolist(),
            "units": {"accel": "ms2", "gyro": "rads", "time": "s"},
        },
    )


def gen_duplicate_ts() -> None:
    rng = np.random.default_rng(2024)
    fs = 100.0
    t1 = np.arange(0, 10, 1 / fs)
    # 第二个采集段从 +60s 开始（缺口 50s，远超 5*中位间隔）
    t2 = np.arange(60.0, 70.0, 1 / fs)
    t = np.concatenate([t1, t2])
    b = np.array([0.012, -0.009, 0.003])
    a, g = _static_imu(t, b, rng)
    # 复制两个时间点形成重复样本（数值一致 -> 可合并）
    dup_idx = [500, 1200]
    t = np.insert(t, [i + 1 for i in dup_idx], t[dup_idx])
    a = np.insert(a, [i + 1 for i in dup_idx], a[dup_idx], axis=0)
    g = np.insert(g, [i + 1 for i in dup_idx], g[dup_idx], axis=0)
    _save(
        "duplicate_ts.json",
        {
            "timestamps": t.tolist(),
            "accel": a.tolist(),
            "gyro": g.tolist(),
            "units": {"accel": "ms2", "gyro": "rads", "time": "s"},
        },
    )


def gen_empty_static() -> None:
    rng = np.random.default_rng(5)
    fs = 100.0
    t = np.arange(0, 20, 1 / fs)
    a = rng.normal(0, 2.0, (len(t), 3))
    a[:, 2] += G * 0.3  # 持续运动，重力幅值也不满足
    g = rng.normal(0, 0.5, (len(t), 3))
    _save(
        "empty_static.json",
        {
            "timestamps": t.tolist(),
            "accel": a.tolist(),
            "gyro": g.tolist(),
            "units": {"accel": "ms2", "gyro": "rads", "time": "s"},
        },
    )


def main() -> None:
    gen_temp_drift()
    gen_sudden_motion()
    gen_spikes()
    gen_duplicate_ts()
    gen_empty_static()


if __name__ == "__main__":
    main()
