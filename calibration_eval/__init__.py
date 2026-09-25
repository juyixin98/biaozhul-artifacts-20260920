"""二分类概率校准评估:Brier 分数、对数损失、ECE 分箱。

纯 NumPy 实现,不依赖外部模型或数据。
"""

from calibration_eval.metrics import (
    DEFAULT_EPSILON,
    DEFAULT_N_BINS,
    VALID_ENDPOINT_STRATEGIES,
    brier_score,
    calibration_bins,
    evaluate,
    expected_calibration_error,
    log_loss,
)
from calibration_eval.validation import validate_inputs

__all__ = [
    "DEFAULT_EPSILON",
    "DEFAULT_N_BINS",
    "VALID_ENDPOINT_STRATEGIES",
    "brier_score",
    "calibration_bins",
    "evaluate",
    "expected_calibration_error",
    "log_loss",
    "validate_inputs",
]

__version__ = "0.1.0"
