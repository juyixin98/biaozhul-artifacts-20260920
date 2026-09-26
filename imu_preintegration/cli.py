"""JSON 入口：从 JSON 请求文件读取 IMU 数据，输出积分结果 JSON。

用法：
    python -m imu_preintegration.cli request.json [-o result.json]

请求格式见 README 与 examples/ 目录。
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

import numpy as np

from .integrator import ImuIntegrator, IntegrationConfig


def build_config(config_dict: dict[str, Any]) -> IntegrationConfig:
    """由请求中的 config 段构造 IntegrationConfig（未知字段报错）。"""
    known = {"gravity", "gyro_bias", "accel_bias", "max_dt", "max_gyro_rad_s"}
    unknown = set(config_dict) - known
    if unknown:
        raise ValueError(f"unknown config fields: {sorted(unknown)}")
    kwargs: dict[str, Any] = {}
    for name in ("gravity", "gyro_bias", "accel_bias"):
        if name in config_dict:
            kwargs[name] = np.asarray(config_dict[name], dtype=float)
    for name in ("max_dt", "max_gyro_rad_s"):
        if name in config_dict:
            kwargs[name] = float(config_dict[name])
    return IntegrationConfig(**kwargs)


def run_request(request: dict[str, Any]) -> dict[str, Any]:
    """执行一次积分请求，返回可 JSON 序列化的结果字典。"""
    if "imu" not in request:
        raise ValueError("request must contain an 'imu' section")
    imu = request["imu"]
    for key in ("timestamps", "gyro", "accel"):
        if key not in imu:
            raise ValueError(f"request.imu must contain '{key}'")

    config = build_config(request.get("config", {}))
    initial = request.get("initial", {})
    unknown_initial = set(initial) - {"orientation", "velocity", "position"}
    if unknown_initial:
        raise ValueError(f"unknown initial fields: {sorted(unknown_initial)}")

    integrator = ImuIntegrator(config)
    result = integrator.integrate(
        np.asarray(imu["timestamps"], dtype=float),
        np.asarray(imu["gyro"], dtype=float),
        np.asarray(imu["accel"], dtype=float),
        initial_orientation=initial.get("orientation"),
        initial_velocity=initial.get("velocity"),
        initial_position=initial.get("position"),
    )
    return result.to_dict()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="imu-preintegrate",
        description="Offline IMU integration with known gravity and fixed biases (JSON in/out).",
    )
    parser.add_argument("request", help="path to the JSON request file")
    parser.add_argument(
        "-o",
        "--output",
        help="path to write the JSON result (default: stdout)",
        default=None,
    )
    args = parser.parse_args(argv)

    try:
        with open(args.request, "r", encoding="utf-8") as fh:
            request = json.load(fh)
        result = run_request(request)
    except (OSError, json.JSONDecodeError, ValueError, KeyError) as exc:
        error = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        print(json.dumps(error, indent=2), file=sys.stderr)
        return 1

    payload = json.dumps(result, indent=2)
    if args.output:
        with open(args.output, "w", encoding="utf-8") as fh:
            fh.write(payload + "\n")
    else:
        print(payload)
    return 0


if __name__ == "__main__":
    sys.exit(main())
