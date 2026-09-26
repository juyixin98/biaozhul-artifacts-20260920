"""异常跳变诊断。

对积分过程中的每个采样步做合理性检查，输出结构化诊断标记。
所有诊断都是“启发式异常提示”，不是真值校验——编码器只能测轮，
测不到轮地接触，因此打滑等故障无法仅凭编码器可靠识别
（详见 README 的局限性说明）。
"""

from __future__ import annotations

from typing import List

import numpy as np

from .config import DiagnosticThresholds

# 诊断标记常量
FLAG_WRAP_LEFT = "counter_wrap_left"
FLAG_WRAP_RIGHT = "counter_wrap_right"
FLAG_TIME_GAP = "time_gap"
FLAG_NON_MONOTONIC_TIME = "non_monotonic_time"
FLAG_VELOCITY_SPIKE = "velocity_spike"
FLAG_ANGULAR_SPIKE = "angular_spike"


def diagnose_steps(
    timestamps: np.ndarray,
    ds_center: np.ndarray,
    dtheta: np.ndarray,
    wrap_left: np.ndarray,
    wrap_right: np.ndarray,
    thresholds: DiagnosticThresholds,
) -> List[List[str]]:
    """逐步诊断，返回与样本等长的标记列表（第 0 个样本恒为空）。

    Args:
        timestamps: 形状 (N,) 的时间戳 (s)，要求单调递增。
        ds_center: 形状 (N,) 的中心弧长增量 (m)。
        dtheta: 形状 (N,) 的航向增量 (rad)。
        wrap_left / wrap_right: 形状 (N,) 的回绕标记。
        thresholds: 诊断阈值。
    """
    n = timestamps.shape[0]
    flags: List[List[str]] = [[] for _ in range(n)]

    dt = np.diff(timestamps)
    non_monotonic = dt <= 0
    if np.any(non_monotonic):
        for idx in np.nonzero(non_monotonic)[0] + 1:
            flags[idx].append(FLAG_NON_MONOTONIC_TIME)
    # 非单调处 dt 无物理意义，速度诊断中按 inf 处理以触发 spike 标记
    safe_dt = np.where(non_monotonic, np.inf, dt)

    time_gap = dt > thresholds.max_dt
    for idx in np.nonzero(time_gap)[0] + 1:
        flags[idx].append(FLAG_TIME_GAP)

    with np.errstate(divide="ignore", invalid="ignore"):
        linear_velocity = np.abs(ds_center[1:]) / safe_dt
        angular_velocity = np.abs(dtheta[1:]) / safe_dt
    for idx in np.nonzero(linear_velocity > thresholds.max_linear_velocity)[0] + 1:
        flags[idx].append(FLAG_VELOCITY_SPIKE)
    for idx in np.nonzero(angular_velocity > thresholds.max_angular_velocity)[0] + 1:
        flags[idx].append(FLAG_ANGULAR_SPIKE)

    for idx in np.nonzero(wrap_left)[0]:
        flags[idx].append(FLAG_WRAP_LEFT)
    for idx in np.nonzero(wrap_right)[0]:
        flags[idx].append(FLAG_WRAP_RIGHT)

    return flags
