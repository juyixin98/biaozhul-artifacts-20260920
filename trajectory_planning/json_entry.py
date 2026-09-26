"""JSON 命令行入口：从文件或标准输入读取请求，输出规划结果 JSON。

用法：
    python -m trajectory_planning.json_entry request.json
    python -m trajectory_planning.json_entry request.json --sample-dt 0.05
    cat request.json | python -m trajectory_planning.json_entry
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Optional

import numpy as np

from .kinematics import InfeasibleTrajectory
from .planner import build_profile
from .profile import sample_grid
from .synthetic import compare_odometry, simulate_odometry


def _to_jsonable(value):
    """递归把 numpy 标量/数组转成可 JSON 序列化的 Python 对象。"""
    if isinstance(value, np.ndarray):
        return [_to_jsonable(v) for v in value.tolist()]
    if isinstance(value, np.generic):
        return value.item()
    if isinstance(value, dict):
        return {k: _to_jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_to_jsonable(v) for v in value]
    return value


def _attach_samples(result: dict, profile, sample_dt: float) -> None:
    """按用户要求附加稠密采样（合成数据）。"""
    grid = sample_grid(profile, sample_dt)
    result["samples"] = {
        "dt": sample_dt,
        "t": grid["t"],
        "position": grid["position"],
        "speed": grid["speed"],
        "tangential_accel": grid["tangential_accel"],
    }


def _attach_synthetic_sensor(
    result: dict, profile, sensor_cfg: dict
) -> None:
    """附加带噪合成里程计读数与误差统计。"""
    odo = simulate_odometry(
        profile,
        dt=float(sensor_cfg.get("dt", 0.01)),
        position_noise_std=float(sensor_cfg.get("position_noise_std", 0.001)),
        speed_noise_std=float(sensor_cfg.get("speed_noise_std", 0.01)),
        seed=int(sensor_cfg.get("seed", 0)),
    )
    result["synthetic_odometry"] = {
        "t": odo["t"],
        "position_measured": odo["position_measured"],
        "speed_measured": odo["speed_measured"],
        "error_stats": compare_odometry(odo),
    }


def run_request(request: dict) -> dict:
    """执行单个请求并返回可序列化结果；失败时返回错误结构。"""
    from .planner import plan_from_request  # 延迟导入避免循环

    try:
        result = plan_from_request(request)
    except (ValueError, InfeasibleTrajectory, KeyError) as exc:
        return {"status": "error", "error_type": type(exc).__name__, "message": str(exc)}

    # 需要档案对象时重新规划一次（保持 plan_from_request 无 numpy 依赖）
    needs_profile = (
        request.get("sample_dt") is not None or request.get("synthetic_sensor")
    )
    if needs_profile:
        profile, _ = build_profile(
            request["points"],
            request.get("dynamics", {}),
            request.get("corner"),
        )
        if request.get("sample_dt") is not None:
            _attach_samples(result, profile, float(request["sample_dt"]))
        if request.get("synthetic_sensor"):
            _attach_synthetic_sensor(result, profile, request["synthetic_sensor"])
    return result


def main(argv: Optional[list] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="trajectory-plan",
        description="运动轨迹速度约束离线计算（纯后端，合成数据）",
    )
    parser.add_argument(
        "request", nargs="?", default=None,
        help="请求 JSON 文件路径；省略时从标准输入读取",
    )
    parser.add_argument(
        "--pretty", action="store_true", help="缩进输出 JSON"
    )
    args = parser.parse_args(argv)

    try:
        if args.request is None:
            request = json.load(sys.stdin)
        else:
            with open(args.request, "r", encoding="utf-8") as fh:
                request = json.load(fh)
    except (OSError, json.JSONDecodeError) as exc:
        result = {"status": "error", "error_type": "BadRequest", "message": str(exc)}
        print(json.dumps(result, ensure_ascii=False, indent=2 if args.pretty else None))
        return 2

    result = _to_jsonable(run_request(request))
    print(json.dumps(result, ensure_ascii=False, indent=2 if args.pretty else None))
    return 0 if result.get("status") == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(main())
