"""IMU 样本校验与规范化。

处理边界输入，所有错误抛出 :class:`IMUDataError`，携带稳定的 ``code``
字符串，供 JSON 入口序列化为结构化错误。

检测覆盖：
- 结构错误（空序列、缺字段、非数值、NaN/Inf、向量长度不为 3）
- 重复时间戳（duplicate_timestamp）
- 时间倒序（non_monotonic_time）
- 缺样/掉帧（missing_samples，相邻间隔超过 ``max_dt``）
- 角速度单位错误（gyro_unit_error，rad/s 数据幅值超出合理量程时，
  典型原因是把 deg/s 数值误标为 rad/s）
"""

from __future__ import annotations

from typing import Any

import numpy as np

# 常见 MEMS IMU 量程上限：约 ±2000 dps ≈ ±34.9 rad/s
DEFAULT_GYRO_LIMIT_RAD_S = 35.0
DEFAULT_GYRO_LIMIT_DEG_S = 2000.0

REQUIRED_KEYS = ("t", "gyro", "accel")


class IMUDataError(ValueError):
    """样本校验错误。``code`` 为机器可读的稳定错误码，``index`` 为出错样本下标。"""

    def __init__(self, message: str, code: str = "invalid_data", index: int | None = None):
        super().__init__(message)
        self.code = code
        self.index = index

    def to_dict(self) -> dict[str, Any]:
        d = {"code": self.code, "message": str(self)}
        if self.index is not None:
            d["index"] = self.index
        return d


def _as_vec3(value: Any, index: int, field: str) -> np.ndarray:
    if not isinstance(value, (list, tuple, np.ndarray)):
        raise IMUDataError(
            f"样本 {index} 的 {field} 必须是长度 3 的数组", "invalid_field", index
        )
    if len(value) != 3:
        raise IMUDataError(
            f"样本 {index} 的 {field} 长度为 {len(value)}，应为 3", "invalid_field", index
        )
    try:
        vec = np.asarray(value, dtype=float)
    except (TypeError, ValueError) as exc:
        raise IMUDataError(
            f"样本 {index} 的 {field} 含非数值元素", "invalid_field", index
        ) from exc
    if not np.all(np.isfinite(vec)):
        raise IMUDataError(
            f"样本 {index} 的 {field} 含 NaN 或 Inf", "non_finite", index
        )
    return vec


def _coerce_raw(samples: Any) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    if not isinstance(samples, (list, tuple, np.ndarray)):
        raise IMUDataError("samples 必须是样本对象的数组", "invalid_data")
    if len(samples) == 0:
        raise IMUDataError("samples 不能为空", "empty_data")

    times, gyros, accels = [], [], []
    for i, raw in enumerate(samples):
        if not isinstance(raw, dict):
            raise IMUDataError(f"样本 {i} 必须是对象/字典", "invalid_field", i)
        missing = [k for k in REQUIRED_KEYS if k not in raw]
        if missing:
            raise IMUDataError(
                f"样本 {i} 缺少字段: {', '.join(missing)}", "missing_field", i
            )
        t = raw["t"]
        if isinstance(t, bool) or not isinstance(t, (int, float, np.integer, np.floating)):
            raise IMUDataError(f"样本 {i} 的 t 必须是数值标量", "invalid_field", i)
        t = float(t)
        if not np.isfinite(t):
            raise IMUDataError(f"样本 {i} 的 t 为 NaN 或 Inf", "non_finite", i)
        times.append(t)
        gyros.append(_as_vec3(raw["gyro"], i, "gyro"))
        accels.append(_as_vec3(raw["accel"], i, "accel"))

    return np.asarray(times), np.asarray(gyros), np.asarray(accels)


def prepare_samples(
    samples: Any,
    gyro_unit: str = "rad/s",
    sort: bool = False,
    drop_duplicates: bool = False,
    max_dt: float | None = None,
    gyro_limit_rad_s: float = DEFAULT_GYRO_LIMIT_RAD_S,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """校验并规范化样本，返回 ``(t, gyro[rad/s], accel)`` 三个浮点数组。

    参数
    ----
    gyro_unit:
        ``"rad/s"``（默认）或 ``"deg/s"``；后者会乘以 pi/180 转换。
    sort:
        为 True 时先按时间戳排序；否则时间倒序直接报错。
    drop_duplicates:
        为 True 时丢弃时间戳重复的样本（保留最先出现的一条）；
        否则遇到重复时间戳报错。
    max_dt:
        若给定，任意相邻时间间隔大于该值即判为缺样/掉帧并报错。
    gyro_limit_rad_s:
        转换为 rad/s 后的角速度幅值上限，超过则按"单位错误"报错
        （rad/s 通道里出现 deg/s 量级数值的典型症状）。
    """
    t, gyro, accel = _coerce_raw(samples)

    if gyro_unit not in ("rad/s", "deg/s"):
        raise IMUDataError(
            f"不支持的 gyro_unit: {gyro_unit!r}，应为 'rad/s' 或 'deg/s'",
            "invalid_unit",
        )

    if sort:
        order = np.argsort(t, kind="stable")
        t, gyro, accel = t[order], gyro[order], accel[order]
    else:
        backwards = np.diff(t) < 0
        if np.any(backwards):
            idx = int(np.argmax(backwards)) + 1
            raise IMUDataError(
                f"样本 {idx} 的时间戳 {t[idx]} 早于前一个样本 {t[idx - 1]}；"
                "如需自动排序请设置 sort=true",
                "non_monotonic_time",
                idx,
            )

    dup = np.diff(t) == 0
    if np.any(dup):
        first_dup = int(np.argmax(dup)) + 1
        if not drop_duplicates:
            raise IMUDataError(
                f"样本 {first_dup} 与前一样本时间戳相同（t={t[first_dup]}）；"
                "如需丢弃重复样本请设置 drop_duplicates=true",
                "duplicate_timestamp",
                first_dup,
            )
        keep = np.concatenate(([True], np.diff(t) > 0))
        t, gyro, accel = t[keep], gyro[keep], accel[keep]

    if max_dt is not None:
        gaps = np.diff(t)
        bad = gaps > float(max_dt)
        if np.any(bad):
            idx = int(np.argmax(bad)) + 1
            raise IMUDataError(
                f"样本 {idx} 与前一样本间隔 {gaps[idx - 1]:.6g}s 超过 max_dt="
                f"{float(max_dt):.6g}s，疑似缺样/掉帧",
                "missing_samples",
                idx,
            )

    if gyro_unit == "deg/s":
        gyro = gyro * (np.pi / 180.0)

    if len(t) >= 2:
        magnitude = np.linalg.norm(gyro, axis=1)
        worst = int(np.argmax(magnitude))
        if magnitude[worst] > float(gyro_limit_rad_s):
            raise IMUDataError(
                f"样本 {worst} 角速度幅值 {magnitude[worst]:.4g} rad/s 超过量程上限 "
                f"{float(gyro_limit_rad_s):.4g} rad/s；若原始数据单位是 deg/s，"
                "请设置 gyro_unit='deg/s'",
                "gyro_unit_error",
                worst,
            )

    return t, gyro, accel


def validate_samples(samples: Any, **kwargs: Any) -> dict[str, Any]:
    """仅做校验，返回结构化摘要；不抛错场景供入口/测试使用。"""
    t, gyro, accel = prepare_samples(samples, **kwargs)
    return {
        "ok": True,
        "count": int(len(t)),
        "duration": float(t[-1] - t[0]) if len(t) >= 2 else 0.0,
        "gyro_unit": "rad/s",
    }
