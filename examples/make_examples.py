"""生成示例输入：一段真实的二维恒速 + 转弯轨迹，导出有序/乱序 JSONL。

运行：
    python -m examples.make_examples
输出：
    examples/data/ordered.jsonl      严格按测量时间排序
    examples/data/shuffled.jsonl     同批消息故意乱序（窗口内到达）
    examples/data/bad_covariance.jsonl
"""
from __future__ import annotations

import json
import os

import numpy as np

RNG = np.random.default_rng(20260923)

HERE = os.path.dirname(__file__)
DATA_DIR = os.path.join(HERE, "data")


def ground_truth(t: float) -> tuple[np.ndarray, np.ndarray]:
    """位置 + 速度真值。前 6s 恒速，之后做匀速圆周转弯。"""
    if t < 6.0:
        pos = np.array([1.0 * t, 0.5 * t])
        vel = np.array([1.0, 0.5])
    else:
        tau = t - 6.0
        omega = 0.35
        c = np.array([6.0, 3.0])
        r_vec = np.array(
            [np.sin(omega * tau) / omega, (1.0 - np.cos(omega * tau)) / omega]
        )
        pos = c + np.array([1.0, 0.0]) * r_vec[0] + np.array([0.0, 1.0]) * r_vec[1]
        vel = np.array([np.cos(omega * tau), np.sin(omega * tau)])
    return pos, vel


def make_messages() -> list[dict]:
    messages: list[dict] = []
    # 里程计 10Hz，GNSS 2Hz，时间错开避免完全同刻
    odo_times = np.round(np.arange(0.1, 12.0, 0.1), 3)
    gnss_times = np.round(np.arange(0.25, 12.0, 0.5), 3)

    R_odo = [[0.02, 0.0], [0.0, 0.02]]
    R_gnss = [[0.6, 0.03], [0.03, 0.5]]

    for i, t in enumerate(odo_times):
        _, vel = ground_truth(float(t))
        z = vel + RNG.normal(scale=[0.12, 0.12])
        messages.append(
            {
                "id": f"odo-{i:04d}",
                "type": "odometry",
                "time": float(t),
                "measurement": [float(z[0]), float(z[1])],
                "R": R_odo,
                "seq": 0,
            }
        )
    for i, t in enumerate(gnss_times):
        pos, _ = ground_truth(float(t))
        z = pos + RNG.normal(scale=[0.5, 0.5])
        messages.append(
            {
                "id": f"gnss-{i:04d}",
                "type": "gnss",
                "time": float(t),
                "measurement": [float(z[0]), float(z[1])],
                "R": R_gnss,
                "seq": 0,
            }
        )
    return messages


def order_key(m: dict) -> tuple[float, int, str]:
    rank = 0 if m["type"] == "odometry" else 1
    return (m["time"], m.get("seq", 0), f"{rank}:{m['id']}")


def shuffled_view(messages: list[dict]) -> list[dict]:
    """窗口内乱序：保持首条（最早测量）在最前，其余按 0.4~1.8s 的“到达延迟”重排。
    任意测量的到达顺序对应的最早测量时间差都 < 2s，因此全部落在迟到窗口内。"""
    ordered = sorted(messages, key=order_key)
    first, rest = ordered[0], ordered[1:]
    arrivals = []
    for m in rest:
        lag = float(RNG.uniform(0.4, 1.8))
        arrivals.append((m["time"] + lag, m))
    arrivals.sort(key=lambda x: (x[0], x[1]["id"]))
    return [first] + [m for _, m in arrivals]


def main() -> None:
    os.makedirs(DATA_DIR, exist_ok=True)
    messages = make_messages()
    ordered = sorted(messages, key=order_key)
    shuffled = shuffled_view(messages)

    with open(os.path.join(DATA_DIR, "ordered.jsonl"), "w", encoding="utf-8") as f:
        for m in ordered:
            f.write(json.dumps(m, ensure_ascii=False) + "\n")

    with open(os.path.join(DATA_DIR, "shuffled.jsonl"), "w", encoding="utf-8") as f:
        for m in shuffled:
            f.write(json.dumps(m, ensure_ascii=False) + "\n")

    bad = [
        # 非对称协方差
        {
            "id": "bad-asym-1",
            "type": "gnss",
            "time": 1.0,
            "measurement": [1.0, 0.5],
            "R": [[0.6, 0.2], [0.03, 0.5]],
            "seq": 0,
        },
        # 非半正定协方差（负特征值）
        {
            "id": "bad-psd-1",
            "type": "odometry",
            "time": 1.1,
            "measurement": [1.0, 0.5],
            "R": [[0.1, 0.5], [0.5, -0.2]],
            "seq": 0,
        },
    ]
    with open(
        os.path.join(DATA_DIR, "bad_covariance.jsonl"), "w", encoding="utf-8"
    ) as f:
        for m in bad:
            f.write(json.dumps(m) + "\n")

    print(
        f"wrote {len(ordered)} ordered, {len(shuffled)} shuffled, "
        f"2 bad-covariance messages to {DATA_DIR}"
    )


if __name__ == "__main__":
    main()
