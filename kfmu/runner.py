"""批量序列运行：给定模型、初值与一串量测，逐步 predict+update。"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from . import config
from .errors import DimensionError, KalmanError
from .filter import KalmanFilter, _parse_available
from .model import LinearKalmanModel


@dataclass
class BatchStepRecord:
    """批量运行中单步记录（下标从 0 开始，对应第 1 个量测）。"""

    index: int
    status: str  # "updated" | "predicted_only" | "error"
    x: np.ndarray | None = None
    P: np.ndarray | None = None
    available: list[bool] | None = None
    innovation: list[float] | None = None
    error_code: str | None = None
    error_message: str | None = None


@dataclass
class BatchResult:
    records: list[BatchStepRecord] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return all(r.status != "error" for r in self.records)


def run_batch(
    model: LinearKalmanModel,
    x0,
    P0,
    measurements,
    masks=None,
    controls=None,
    *,
    max_steps: int | None = None,
) -> BatchResult:
    """顺序执行滤波。

    参数
    ----
    measurements: 长度 T 的序列，每项为长度 m 的向量；分量为 NaN 表示缺测；
                  整项为 None 表示该步完全无量测（纯预测）。
    masks: 可选，长度 T 的布尔向量序列，与 NaN 取交集。
    controls: 可选，长度 T 的控制输入序列（模型需带 B）。

    单步出错（如奇异创新协方差）时：
      * 记录 ``status="error"``、错误码与信息；
      * 滤波器保留该步预测后的先验，继续下一步（对应"跳过该次量测更新"）。
    模型/初值层面的错误在进入循环前直接抛出。
    """
    limit = max_steps if max_steps is not None else config.MAX_STEPS
    measurements = list(measurements)
    if len(measurements) > limit:
        raise DimensionError(f"步数 {len(measurements)} 超过上限 {limit}")

    if masks is not None:
        masks = list(masks)
        if len(masks) != len(measurements):
            raise DimensionError("masks 与 measurements 长度不一致")
    if controls is not None:
        controls = list(controls)
        if len(controls) != len(measurements):
            raise DimensionError("controls 与 measurements 长度不一致")

    kf = KalmanFilter(model, x0, P0)
    result = BatchResult()

    for k, z in enumerate(measurements):
        u = controls[k] if controls is not None else None
        mask = masks[k] if masks is not None else None
        try:
            pred = kf.predict(u=u)  # 先得到先验；更新失败时保留它
        except KalmanError:
            # 预测步失败属于致命问题（协方差已坏），记录并终止
            result.records.append(
                BatchStepRecord(index=k, status="error",
                                available=(_mask_list(mask, model.meas_dim)),
                                error_code="predict_failed",
                                error_message="预测步失败，序列终止")
            )
            break

        if z is None:
            result.records.append(_ok_record(k, pred, None))
            continue

        # 统一解析 NaN 与布尔掩码，成功/失败记录都能反映真实可用分量
        try:
            avail = _parse_available(z, mask, model.meas_dim)
        except KalmanError as exc:
            result.records.append(
                BatchStepRecord(
                    index=k, status="error",
                    x=pred.x.copy(), P=pred.P.copy(),
                    available=(_mask_list(mask, model.meas_dim)),
                    error_code=getattr(exc, "code", "kalman_error"),
                    error_message=str(exc),
                )
            )
            continue

        try:
            step = kf.update(z, available=avail)
        except KalmanError as exc:
            # 量测更新失败（如奇异 S）：保留先验，标记错误，继续
            result.records.append(
                BatchStepRecord(
                    index=k,
                    status="error",
                    x=pred.x.copy(),
                    P=pred.P.copy(),
                    available=[bool(v) for v in avail],
                    error_code=getattr(exc, "code", "kalman_error"),
                    error_message=str(exc),
                )
            )
            continue

        result.records.append(_ok_record(k, step, z))

    return result


def _mask_list(mask, m: int) -> list[bool] | None:
    if mask is None:
        return None
    return [bool(v) for v in np.asarray(mask).reshape(-1)]


def _ok_record(index: int, step, z) -> BatchStepRecord:
    return BatchStepRecord(
        index=index,
        status=step.status,
        x=step.x.copy(),
        P=step.P.copy(),
        available=[bool(v) for v in step.available],
        innovation=(None if step.innovation is None
                    else [float(v) for v in np.atleast_1d(step.innovation)]),
    )
