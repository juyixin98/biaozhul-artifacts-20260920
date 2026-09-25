"""kfmu —— 支持部分观测缺失的线性卡尔曼滤波（纯后端，NumPy 实现）。"""

from .errors import (
    DimensionError,
    InvalidValueError,
    KalmanError,
    MatrixPropertyError,
    NumericalStabilityError,
)
from .filter import KalmanFilter, StepResult, predict, update
from .model import LinearKalmanModel
from .runner import BatchResult, BatchStepRecord, run_batch

__all__ = [
    "KalmanError",
    "DimensionError",
    "InvalidValueError",
    "MatrixPropertyError",
    "NumericalStabilityError",
    "LinearKalmanModel",
    "KalmanFilter",
    "StepResult",
    "predict",
    "update",
    "run_batch",
    "BatchResult",
    "BatchStepRecord",
]
