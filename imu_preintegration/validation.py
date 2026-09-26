"""IMU 数据流校验：缺样、重复时间戳、角速度单位可疑等检查。

校验结果分为错误（error）与警告（warning）：
- error：数据本身非法（如重复/回退的时间戳、长度不一致），积分器会拒绝或跳过。
- warning：数据可疑但可继续（如缺样导致的时间空洞、角速度幅值疑似 deg/s）。
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

# 默认阈值
DEFAULT_MAX_DT_S = 0.05  # 相邻样本时间间隔上限，超过视为缺样
DEFAULT_MAX_GYRO_RAD_S = 4.0 * np.pi  # 合理角速度上限（约 720 deg/s）


@dataclass(frozen=True)
class ValidationIssue:
    """单条校验发现。"""

    severity: str  # "error" | "warning"
    code: str  # 机器可读代码，如 "duplicate_timestamp"
    message: str  # 人类可读描述
    index: int | None = None  # 关联的样本下标（若适用）


@dataclass
class ValidationReport:
    """校验报告。"""

    issues: list[ValidationIssue] = field(default_factory=list)

    @property
    def errors(self) -> list[ValidationIssue]:
        return [i for i in self.issues if i.severity == "error"]

    @property
    def warnings(self) -> list[ValidationIssue]:
        return [i for i in self.issues if i.severity == "warning"]

    @property
    def ok(self) -> bool:
        """无 error 即视为可积分（warning 不阻塞）。"""
        return not self.errors

    def to_dict(self) -> dict:
        return {
            "ok": self.ok,
            "issues": [
                {
                    "severity": i.severity,
                    "code": i.code,
                    "message": i.message,
                    "index": i.index,
                }
                for i in self.issues
            ],
        }


def validate_imu_stream(
    timestamps: np.ndarray,
    gyro: np.ndarray,
    accel: np.ndarray,
    *,
    max_dt: float = DEFAULT_MAX_DT_S,
    max_gyro_rad_s: float = DEFAULT_MAX_GYRO_RAD_S,
) -> ValidationReport:
    """校验 IMU 数据流。

    参数：
        timestamps: 形状 (N,) 的时间戳，单位秒，要求严格递增。
        gyro: 形状 (N, 3) 的角速度，单位 rad/s。
        accel: 形状 (N, 3) 的比力测量，单位 m/s^2。
        max_dt: 相邻样本间隔上限，超过记缺样 warning。
        max_gyro_rad_s: 角速度幅值上限，超过记单位可疑 warning（可能误用 deg/s）。
    """
    report = ValidationReport()
    timestamps = np.asarray(timestamps, dtype=float)
    gyro = np.asarray(gyro, dtype=float)
    accel = np.asarray(accel, dtype=float)

    n = timestamps.shape[0]
    if gyro.shape != (n, 3):
        report.issues.append(
            ValidationIssue(
                "error",
                "shape_mismatch",
                f"gyro shape {gyro.shape} does not match ({n}, 3)",
            )
        )
    if accel.shape != (n, 3):
        report.issues.append(
            ValidationIssue(
                "error",
                "shape_mismatch",
                f"accel shape {accel.shape} does not match ({n}, 3)",
            )
        )
    if n < 2:
        report.issues.append(
            ValidationIssue("error", "too_few_samples", f"need >= 2 samples, got {n}")
        )
        return report
    if not (np.all(np.isfinite(timestamps)) and np.all(np.isfinite(gyro)) and np.all(np.isfinite(accel))):
        report.issues.append(
            ValidationIssue("error", "non_finite", "timestamps/gyro/accel contain NaN or inf")
        )

    dt = np.diff(timestamps)
    for k in range(dt.shape[0]):
        if dt[k] == 0.0:
            report.issues.append(
                ValidationIssue(
                    "error",
                    "duplicate_timestamp",
                    f"duplicate timestamp at index {k + 1} (t={timestamps[k + 1]})",
                    index=k + 1,
                )
            )
        elif dt[k] < 0.0:
            report.issues.append(
                ValidationIssue(
                    "error",
                    "non_monotonic_timestamp",
                    f"timestamp goes backward at index {k + 1} (dt={dt[k]})",
                    index=k + 1,
                )
            )
        elif dt[k] > max_dt:
            report.issues.append(
                ValidationIssue(
                    "warning",
                    "sample_gap",
                    f"missing samples suspected between index {k} and {k + 1} "
                    f"(dt={dt[k]:.6f}s > max_dt={max_dt}s)",
                    index=k + 1,
                )
            )

    if gyro.shape == (n, 3) and n > 0:
        gyro_norm = np.linalg.norm(gyro, axis=1)
        median_norm = float(np.median(gyro_norm))
        peak_norm = float(np.max(gyro_norm))
        if median_norm > max_gyro_rad_s:
            report.issues.append(
                ValidationIssue(
                    "warning",
                    "gyro_unit_suspect",
                    f"median gyro norm {median_norm:.3f} rad/s exceeds "
                    f"{max_gyro_rad_s:.3f} rad/s; input may be in deg/s",
                )
            )
        elif peak_norm > max_gyro_rad_s:
            report.issues.append(
                ValidationIssue(
                    "warning",
                    "gyro_unit_suspect",
                    f"peak gyro norm {peak_norm:.3f} rad/s exceeds "
                    f"{max_gyro_rad_s:.3f} rad/s; check units (rad/s expected)",
                )
            )

    return report
