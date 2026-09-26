"""异常跳变与数据质量诊断。

可检测的问题（仅凭编码器可观测的）：
- 时间戳非单调 / 重复
- 采样间隔过大（丢样）
- 隐含速度超过物理上限（疑似打滑、计数错误或设备故障）
- 计数器回绕（信息级，属正常事件，仅记录）

无法检测的问题在 README「局限性」中明确说明。
"""

from __future__ import annotations

from dataclasses import asdict, dataclass


@dataclass(frozen=True)
class Diagnostic:
    """单条诊断记录。"""

    index: int  # 触发诊断的采样下标（samples 数组下标）
    t: float
    type: str
    severity: str  # "info" | "warning" | "error"
    message: str

    def to_dict(self) -> dict:
        return asdict(self)


def check_timestamps(
    timestamps: list[float], max_sample_period_s: float
) -> list[Diagnostic]:
    """检查时间戳单调性与采样间隔。"""
    diagnostics: list[Diagnostic] = []
    for i in range(1, len(timestamps)):
        dt = timestamps[i] - timestamps[i - 1]
        if dt <= 0:
            diagnostics.append(
                Diagnostic(
                    index=i,
                    t=timestamps[i],
                    type="timestamp_nonmonotonic",
                    severity="error",
                    message=(
                        f"时间戳非递增: dt={dt:.6f}s，"
                        "该步积分结果被污染，请检查数据源"
                    ),
                )
            )
        elif dt > max_sample_period_s:
            diagnostics.append(
                Diagnostic(
                    index=i,
                    t=timestamps[i],
                    type="sample_gap",
                    severity="warning",
                    message=(
                        f"采样间隔 dt={dt:.3f}s 超过阈值 "
                        f"{max_sample_period_s:.3f}s，疑似丢样；"
                        "期间按匀速模型积分，误差可能较大"
                    ),
                )
            )
    return diagnostics


def check_step_velocity(
    index: int,
    t: float,
    dt: float,
    d_s_m: float,
    d_theta_rad: float,
    max_linear_velocity_mps: float,
    max_angular_velocity_rps: float,
) -> list[Diagnostic]:
    """检查单步隐含速度是否超出物理上限（疑似跳变/打滑）。"""
    if dt <= 0:
        return []  # 时间戳问题已由 check_timestamps 报告
    diagnostics: list[Diagnostic] = []
    linear = abs(d_s_m) / dt
    angular = abs(d_theta_rad) / dt
    if linear > max_linear_velocity_mps:
        diagnostics.append(
            Diagnostic(
                index=index,
                t=t,
                type="velocity_jump",
                severity="warning",
                message=(
                    f"隐含线速度 {linear:.3f} m/s 超过上限 "
                    f"{max_linear_velocity_mps:.3f} m/s，"
                    "疑似车轮打滑、计数跳变或回绕误判"
                ),
            )
        )
    if angular > max_angular_velocity_rps:
        diagnostics.append(
            Diagnostic(
                index=index,
                t=t,
                type="angular_velocity_jump",
                severity="warning",
                message=(
                    f"隐含角速度 {angular:.3f} rad/s 超过上限 "
                    f"{max_angular_velocity_rps:.3f} rad/s，"
                    "疑似单侧打滑或计数异常"
                ),
            )
        )
    return diagnostics
