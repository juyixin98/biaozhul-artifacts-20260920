"""里程计主流程：采样序列 -> 位姿轨迹 + 诊断。"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from .config import RobotParams
from .diagnostics import Diagnostic, check_step_velocity, check_timestamps
from .encoder import unwrap_count_delta
from .integrator import arc_step


@dataclass(frozen=True)
class Pose:
    """二维位姿。"""

    x_m: float
    y_m: float
    theta_rad: float

    def to_dict(self) -> dict:
        return {"x_m": self.x_m, "y_m": self.y_m, "theta_rad": self.theta_rad}


def run_odometry(
    params: RobotParams,
    timestamps: np.ndarray,
    left_counts: np.ndarray,
    right_counts: np.ndarray,
    initial_pose: Pose | None = None,
) -> dict:
    """对整段采样序列执行里程计积分。

    Args:
        params: 机器人参数。
        timestamps: 采样时间戳（秒），形状 (N,)，严格递增。
        left_counts: 左轮编码器原始读数，形状 (N,)。
        right_counts: 右轮编码器原始读数，形状 (N,)。
        initial_pose: 初始位姿，默认原点。

    Returns:
        可 JSON 序列化的字典：poses / steps / diagnostics / summary。
        poses[i] 对应 samples[i] 时刻的位姿（poses[0] 为初始位姿）。
    """
    timestamps = np.asarray(timestamps, dtype=np.float64)
    left_counts = np.asarray(left_counts, dtype=np.int64)
    right_counts = np.asarray(right_counts, dtype=np.int64)

    if not (len(timestamps) == len(left_counts) == len(right_counts)):
        raise ValueError("timestamps / left_counts / right_counts 长度不一致")
    if len(timestamps) == 0:
        raise ValueError("采样序列为空")

    pose = initial_pose or Pose(0.0, 0.0, 0.0)
    poses: list[dict] = []
    steps: list[dict] = []
    diagnostics: list[Diagnostic] = []

    ts_list = timestamps.tolist()
    diagnostics.extend(check_timestamps(ts_list, params.max_sample_period_s))

    x, y, theta = pose.x_m, pose.y_m, pose.theta_rad
    poses.append({"t": ts_list[0], "x_m": x, "y_m": y, "theta_rad": theta})

    total_distance_m = 0.0
    wrap_count = 0

    for i in range(1, len(ts_list)):
        d_left_ticks, wrapped_left = unwrap_count_delta(
            int(left_counts[i - 1]),
            int(left_counts[i]),
            params.encoder_min,
            params.encoder_max,
        )
        d_right_ticks, wrapped_right = unwrap_count_delta(
            int(right_counts[i - 1]),
            int(right_counts[i]),
            params.encoder_min,
            params.encoder_max,
        )
        if wrapped_left or wrapped_right:
            wrap_count += 1
            which = "左右" if (wrapped_left and wrapped_right) else (
                "左" if wrapped_left else "右"
            )
            diagnostics.append(
                Diagnostic(
                    index=i,
                    t=ts_list[i],
                    type="counter_wrap",
                    severity="info",
                    message=f"{which}轮编码器发生回绕，已按模 {params.encoder_range} 修正",
                )
            )

        d_left_m = d_left_ticks * params.meters_per_tick
        d_right_m = d_right_ticks * params.meters_per_tick
        d_s = (d_left_m + d_right_m) / 2.0
        d_theta = (d_right_m - d_left_m) / params.track_width_m
        dt = ts_list[i] - ts_list[i - 1]

        diagnostics.extend(
            check_step_velocity(
                index=i,
                t=ts_list[i],
                dt=dt,
                d_s_m=d_s,
                d_theta_rad=d_theta,
                max_linear_velocity_mps=params.max_linear_velocity_mps,
                max_angular_velocity_rps=params.max_angular_velocity_rps,
            )
        )

        x, y, theta = arc_step(
            x, y, theta, d_left_m, d_right_m, params.track_width_m
        )
        total_distance_m += abs(d_s)

        poses.append({"t": ts_list[i], "x_m": x, "y_m": y, "theta_rad": theta})
        steps.append(
            {
                "index": i,
                "t": ts_list[i],
                "dt_s": dt,
                "d_left_ticks": d_left_ticks,
                "d_right_ticks": d_right_ticks,
                "d_left_m": d_left_m,
                "d_right_m": d_right_m,
                "d_s_m": d_s,
                "d_theta_rad": d_theta,
            }
        )

    summary = {
        "sample_count": len(ts_list),
        "duration_s": ts_list[-1] - ts_list[0],
        "total_distance_m": total_distance_m,
        "final_pose": poses[-1],
        "counter_wrap_events": wrap_count,
        "diagnostic_counts": {
            "info": sum(1 for d in diagnostics if d.severity == "info"),
            "warning": sum(1 for d in diagnostics if d.severity == "warning"),
            "error": sum(1 for d in diagnostics if d.severity == "error"),
        },
    }

    return {
        "poses": poses,
        "steps": steps,
        "diagnostics": [d.to_dict() for d in diagnostics],
        "summary": summary,
    }
