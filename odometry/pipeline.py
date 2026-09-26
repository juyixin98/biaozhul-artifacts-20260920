"""端到端流水线：校验输入 → 回绕修正 → 距离换算 → 积分 → 诊断。"""

from __future__ import annotations

from typing import Any, Dict, List, Mapping

import numpy as np

from .config import DiagnosticThresholds, RobotParams
from .diagnostics import diagnose_steps
from .integrate import Pose2D, integrate_differential, wheel_distances
from .unwrap import unwrap_counter_deltas

_SAMPLE_REQUIRED_FIELDS = ("t", "left", "right")


def run_odometry(request: Mapping[str, Any]) -> Dict[str, Any]:
    """执行一次离线里程计计算。

    Args:
        request: 请求字典，结构见 README / examples/：
            robot: 机器人参数（必填）
            diagnostics: 诊断阈值（可选）
            initial_pose: {x, y, theta}（可选，默认原点）
            samples: [{"t": 秒, "left": 左计数, "right": 右计数}, ...]（必填）

    Returns:
        结果字典：poses（逐步位姿）、diagnostics（逐步标记）、summary（汇总）。

    Raises:
        ValueError: 输入缺失或非法时。
    """
    _validate_request(request)
    params = RobotParams.from_dict(request["robot"])
    thresholds = DiagnosticThresholds.from_dict(request.get("diagnostics"))

    pose_cfg = request.get("initial_pose") or {}
    initial_pose = Pose2D(
        x=float(pose_cfg.get("x", 0.0)),
        y=float(pose_cfg.get("y", 0.0)),
        theta=float(pose_cfg.get("theta", 0.0)),
    )

    samples = request["samples"]
    timestamps = np.array([float(s["t"]) for s in samples], dtype=np.float64)
    raw_left = np.array([float(s["left"]) for s in samples], dtype=np.float64)
    raw_right = np.array([float(s["right"]) for s in samples], dtype=np.float64)

    deltas_left, wrap_left = unwrap_counter_deltas(raw_left, params.encoder_modulus)
    deltas_right, wrap_right = unwrap_counter_deltas(raw_right, params.encoder_modulus)

    ds_left, ds_right = wheel_distances(
        deltas_left,
        deltas_right,
        params.wheel_diameter_left,
        params.wheel_diameter_right,
        params.ticks_per_revolution,
    )
    ds_center = 0.5 * (ds_right + ds_left)
    dtheta = (ds_right - ds_left) / params.track_width

    poses = integrate_differential(ds_left, ds_right, params.track_width, initial_pose)
    flags = diagnose_steps(
        timestamps, ds_center, dtheta, wrap_left, wrap_right, thresholds
    )

    return _build_result(
        poses=poses,
        timestamps=timestamps,
        ds_center=ds_center,
        dtheta=dtheta,
        flags=flags,
        initial_pose=initial_pose,
    )


def _validate_request(request: Mapping[str, Any]) -> None:
    if not isinstance(request, Mapping):
        raise ValueError("请求必须是 JSON 对象")
    if "robot" not in request:
        raise ValueError("请求缺少 'robot' 配置")
    samples = request.get("samples")
    if not isinstance(samples, list) or len(samples) == 0:
        raise ValueError("'samples' 必须是非空数组")
    for i, sample in enumerate(samples):
        if not isinstance(sample, Mapping):
            raise ValueError(f"samples[{i}] 必须是对象")
        for key in _SAMPLE_REQUIRED_FIELDS:
            if key not in sample:
                raise ValueError(f"samples[{i}] 缺少字段 '{key}'")
            value = sample[key]
            if not isinstance(value, (int, float)) or isinstance(value, bool):
                raise ValueError(f"samples[{i}].{key} 必须是数值")
            if not np.isfinite(value):
                raise ValueError(f"samples[{i}].{key} 必须是有限数值")


def _build_result(
    poses: np.ndarray,
    timestamps: np.ndarray,
    ds_center: np.ndarray,
    dtheta: np.ndarray,
    flags: List[List[str]],
    initial_pose: Pose2D,
) -> Dict[str, Any]:
    pose_list = [
        {
            "t": float(timestamps[i]),
            "x": float(poses[i, 0]),
            "y": float(poses[i, 1]),
            "theta": float(poses[i, 2]),
        }
        for i in range(poses.shape[0])
    ]
    diagnostics = [
        {"t": float(timestamps[i]), "flags": flags[i]} for i in range(len(flags))
    ]
    flagged = sum(1 for f in flags if f)
    summary = {
        "sample_count": int(poses.shape[0]),
        "flagged_samples": flagged,
        "total_distance": float(np.sum(np.abs(ds_center))),
        "total_rotation": float(np.sum(np.abs(dtheta))),
        "final_pose": pose_list[-1] if pose_list else initial_pose.as_dict(),
    }
    return {"poses": pose_list, "diagnostics": diagnostics, "summary": summary}
