"""JSON 入口：解析请求、执行积分/校验、序列化结果。

纯函数 :func:`process_request` 接收 dict 返回 dict，不绑定任何 Web
框架，便于嵌入服务或离线批处理。配套 CLI::

    python -m imu_integrator.cli run request.json -o result.json
    python -m imu_integrator.cli validate request.json
    python -m imu_integrator.cli demo --scenario stationary|rotation|acceleration

请求格式见 examples/request_*.json。
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

import numpy as np

from .integrator import integrate_imu
from .synthetic import (
    stationary_samples,
    constant_rotation_samples,
    constant_acceleration_samples,
)
from .validation import IMUDataError, validate_samples

VEC3_KEYS = ("gravity", "gyro_bias", "accel_bias", "initial_position",
             "initial_velocity", "angular_velocity", "acceleration_world")

PREPARE_DEFAULTS = {
    "gyro_unit": "rad/s",
    "sort": False,
    "drop_duplicates": False,
    "max_dt": None,
}


def _vec3(obj: dict[str, Any], key: str, default: tuple[float, float, float]):
    if key not in obj or obj[key] is None:
        return np.asarray(default, dtype=float)
    value = obj[key]
    if not isinstance(value, (list, tuple)) or len(value) != 3:
        raise IMUDataError(f"{key} 必须是长度 3 的数组", "invalid_field")
    try:
        arr = np.asarray(value, dtype=float)
    except (TypeError, ValueError) as exc:
        raise IMUDataError(f"{key} 含非数值元素", "invalid_field") from exc
    if not np.all(np.isfinite(arr)):
        raise IMUDataError(f"{key} 含 NaN 或 Inf", "non_finite")
    return arr


def _orientation(obj: dict[str, Any]) -> np.ndarray:
    value = obj.get("initial_orientation", [1.0, 0.0, 0.0, 0.0])
    if not isinstance(value, (list, tuple)) or len(value) != 4:
        raise IMUDataError("initial_orientation 必须是长度 4 的四元数 [w,x,y,z]",
                           "invalid_field")
    try:
        q = np.asarray(value, dtype=float)
    except (TypeError, ValueError) as exc:
        raise IMUDataError("initial_orientation 含非数值元素", "invalid_field") from exc
    if not np.all(np.isfinite(q)) or np.linalg.norm(q) < 1e-15:
        raise IMUDataError("initial_orientation 非法（非有限值或零模长）", "non_finite")
    return q / np.linalg.norm(q)


def _prepare_options(options: dict[str, Any] | None) -> dict[str, Any]:
    options = options or {}
    out: dict[str, Any] = {}
    out["gyro_unit"] = str(options.get("gyro_unit", "rad/s"))
    if out["gyro_unit"] not in ("rad/s", "deg/s"):
        raise IMUDataError("options.gyro_unit 只能是 'rad/s' 或 'deg/s'",
                           "invalid_unit")
    out["sort"] = bool(options.get("sort", False))
    out["drop_duplicates"] = bool(options.get("drop_duplicates", False))
    max_dt = options.get("max_dt", None)
    if max_dt is not None:
        if not isinstance(max_dt, (int, float)) or float(max_dt) <= 0:
            raise IMUDataError("options.max_dt 必须是正数", "invalid_field")
        out["max_dt"] = float(max_dt)
    else:
        out["max_dt"] = None
    limit = options.get("gyro_limit_rad_s", None)
    if limit is not None:
        if not isinstance(limit, (int, float)) or float(limit) <= 0:
            raise IMUDataError("options.gyro_limit_rad_s 必须是正数", "invalid_field")
        out["gyro_limit_rad_s"] = float(limit)
    return out


def _build_samples(req: dict[str, Any]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """从请求构造样本。优先使用内联 samples，否则按 scenario 生成合成数据。"""
    options = _prepare_options(req.get("options"))
    if "samples" in req and req["samples"] is not None:
        return req["samples"], options
    if "scenario" not in req or not isinstance(req["scenario"], dict):
        raise IMUDataError(
            "请求必须包含 samples（内联样本）或 scenario（合成数据生成）",
            "invalid_request",
        )
    sc = req["scenario"]
    name = sc.get("name")
    duration = float(sc.get("duration", 1.0))
    dt_field = sc.get("dt", 0.01)
    if isinstance(dt_field, list):
        dt: Any = [float(x) for x in dt_field]
    else:
        dt = float(dt_field)
    common = dict(
        duration=duration,
        dt=dt,
        gravity=_vec3(req, "gravity", (0.0, 0.0, -9.81)),
        gyro_bias=_vec3(sc, "gyro_bias", (0.0, 0.0, 0.0)),
        accel_bias=_vec3(sc, "accel_bias", (0.0, 0.0, 0.0)),
        start_time=float(sc.get("start_time", 0.0)),
    )
    if name == "stationary":
        return stationary_samples(**common), options
    if name == "rotation":
        return constant_rotation_samples(
            angular_velocity=_vec3(sc, "angular_velocity", (0.0, 0.0, 1.0)),
            initial_orientation=_orientation_sc(sc),
            **common,
        ), options
    if name == "acceleration":
        return constant_acceleration_samples(
            acceleration_world=_vec3(sc, "acceleration_world", (1.0, 0.0, 0.0)),
            initial_orientation=_orientation_sc(sc),
            **common,
        ), options
    raise IMUDataError(
        f"未知 scenario.name: {name!r}，支持 stationary/rotation/acceleration",
        "invalid_request",
    )


def _orientation_sc(sc: dict[str, Any]) -> np.ndarray:
    if "initial_orientation" not in sc:
        return np.array([1.0, 0.0, 0.0, 0.0])
    value = sc["initial_orientation"]
    if not isinstance(value, list) or len(value) != 4:
        raise IMUDataError("scenario.initial_orientation 必须是 [w,x,y,z]",
                           "invalid_field")
    q = np.asarray(value, dtype=float)
    return q / np.linalg.norm(q)


def process_request(req: dict[str, Any], *, include_trajectory: bool = True) -> dict[str, Any]:
    """处理一个完整请求。成功返回 ``{"ok": true, ...}``，参数错误返回
    ``{"ok": false, "error": {code,message,...}}``（不抛出）。"""
    if not isinstance(req, dict):
        return {"ok": False, "error": {"code": "invalid_request",
                                       "message": "请求体必须是 JSON 对象"}}
    action = req.get("action", "integrate")
    try:
        samples, options = _build_samples(req)
        if action == "validate":
            summary = validate_samples(samples, **options)
            return {"ok": True, "result": summary}
        if action != "integrate":
            raise IMUDataError(
                f"未知 action: {action!r}，支持 integrate/validate", "invalid_request"
            )

        result = integrate_imu(
            samples,
            gravity=_vec3(req, "gravity", (0.0, 0.0, -9.81)),
            gyro_bias=_vec3(req, "gyro_bias", (0.0, 0.0, 0.0)),
            accel_bias=_vec3(req, "accel_bias", (0.0, 0.0, 0.0)),
            initial_position=_vec3(req, "initial_position", (0.0, 0.0, 0.0)),
            initial_velocity=_vec3(req, "initial_velocity", (0.0, 0.0, 0.0)),
            initial_orientation=_orientation(req),
            **options,
        )
        payload = result.to_dict()
        if not include_trajectory or not req.get("include_trajectory", True):
            payload.pop("trajectory", None)
        return {"ok": True, "result": payload}
    except IMUDataError as exc:
        return {"ok": False, "error": exc.to_dict()}
    except (ValueError, TypeError) as exc:
        return {"ok": False, "error": {"code": "invalid_request", "message": str(exc)}}


DEMO_REQUESTS: dict[str, dict[str, Any]] = {
    "stationary": {
        "action": "integrate",
        "scenario": {"name": "stationary", "duration": 0.5, "dt": 0.05,
                     "gyro_bias": [0.01, -0.02, 0.0]},
        "gyro_bias": [0.01, -0.02, 0.0],
        "include_trajectory": False,
    },
    "rotation": {
        "action": "integrate",
        "scenario": {"name": "rotation", "duration": 1.0, "dt": 0.02,
                     "angular_velocity": [0.0, 0.0, 0.5]},
        "include_trajectory": False,
    },
    "acceleration": {
        "action": "integrate",
        "scenario": {"name": "acceleration", "duration": 1.0,
                     "dt": [0.1, 0.2, 0.3, 0.4],
                     "acceleration_world": [0.5, 0.0, 0.0]},
        "include_trajectory": False,
    },
}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="imu_integrator",
        description="惯性数据预积分子集：离线 IMU 积分 JSON 入口（无硬件、无前端）",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_run = sub.add_parser("run", help="读取 JSON 请求文件并执行积分/校验")
    p_run.add_argument("request_file", help="请求 JSON 路径，- 表示标准输入")
    p_run.add_argument("-o", "--output", help="结果输出路径（默认标准输出）")

    p_val = sub.add_parser("validate", help="仅校验请求中的样本")
    p_val.add_argument("request_file", help="请求 JSON 路径，- 表示标准输入")

    p_demo = sub.add_parser("demo", help="运行内置合成场景")
    p_demo.add_argument("--scenario", choices=sorted(DEMO_REQUESTS),
                        default="stationary")

    args = parser.parse_args(argv)

    if args.cmd == "demo":
        req = DEMO_REQUESTS[args.scenario]
        response = process_request(req)
        print(json.dumps(response, ensure_ascii=False, indent=2))
        return 0 if response.get("ok") else 1

    path = args.request_file
    try:
        if path == "-":
            req = json.load(sys.stdin)
        else:
            with open(path, "r", encoding="utf-8") as fh:
                req = json.load(fh)
    except (OSError, json.JSONDecodeError) as exc:
        print(json.dumps({"ok": False, "error": {
            "code": "io_error", "message": f"无法读取/解析请求: {exc}"}},
            ensure_ascii=False), file=sys.stderr)
        return 2

    if args.cmd == "validate":
        req = {**req, "action": "validate"}
    response = process_request(req)
    text = json.dumps(response, ensure_ascii=False, indent=2)
    if args.cmd == "run" and args.output:
        with open(args.output, "w", encoding="utf-8") as fh:
            fh.write(text + "\n")
    else:
        print(text)
    return 0 if response.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
