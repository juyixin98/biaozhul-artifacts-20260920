"""生成 examples/ 下的请求样例 JSON（合成数据，可重复生成）。

用法：python examples/generate_examples.py
"""

import json
from pathlib import Path

import numpy as np

GRAVITY = np.array([0.0, 0.0, -9.81])
OUT_DIR = Path(__file__).resolve().parent


def write_request(name: str, request: dict) -> None:
    path = OUT_DIR / name
    path.write_text(json.dumps(request, indent=2) + "\n", encoding="utf-8")
    print(f"wrote {path}")


def static_request() -> dict:
    n, dt = 101, 0.01
    return {
        "description": "static IMU, 100 Hz for 1 s; expect zero motion",
        "imu": {
            "timestamps": (np.arange(n) * dt).tolist(),
            "gyro": [[0.0, 0.0, 0.0]] * n,
            "accel": [[0.0, 0.0, 9.81]] * n,
        },
        "config": {"gravity": [0.0, 0.0, -9.81]},
    }


def constant_rotation_request() -> dict:
    n, dt = 201, 0.01
    omega_z = 0.5  # rad/s
    return {
        "description": "constant rotation 0.5 rad/s about z for 2 s; "
        "expect final yaw = 1.0 rad, no translation",
        "imu": {
            "timestamps": (np.arange(n) * dt).tolist(),
            "gyro": [[0.0, 0.0, omega_z]] * n,
            "accel": [[0.0, 0.0, 9.81]] * n,
        },
        "config": {"gravity": [0.0, 0.0, -9.81]},
    }


def constant_accel_request() -> dict:
    n, dt = 201, 0.01
    a_true = np.array([1.0, -0.5, 0.2])
    f = (a_true - GRAVITY).tolist()
    return {
        "description": "constant world accel [1, -0.5, 0.2] m/s^2 for 2 s; "
        "expect v = a*T and p = 0.5*a*T^2",
        "imu": {
            "timestamps": (np.arange(n) * dt).tolist(),
            "gyro": [[0.0, 0.0, 0.0]] * n,
            "accel": [f] * n,
        },
        "config": {"gravity": [0.0, 0.0, -9.81]},
    }


def main() -> None:
    write_request("request_static.json", static_request())
    write_request("request_constant_rotation.json", constant_rotation_request())
    write_request("request_constant_accel.json", constant_accel_request())


if __name__ == "__main__":
    main()
