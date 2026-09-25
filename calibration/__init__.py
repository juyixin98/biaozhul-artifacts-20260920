"""校准误差评估（Calibration Error Evaluation）。

纯 NumPy 实现的二分类概率校准评估后端，提供：

- Brier 分数（加权）
- 对数损失（加权，支持概率端点策略）
- 等宽分箱 ECE（加权）
- JSON 风格的服务层与命令行入口
"""

from .errors import CalibrationError
from .metrics import (
    brier_score,
    expected_calibration_error,
    evaluate_calibration,
    log_loss,
)
from .service import CalibrationService
from .synthetic import make_demo_dataset

__all__ = [
    "CalibrationError",
    "CalibrationService",
    "brier_score",
    "log_loss",
    "expected_calibration_error",
    "evaluate_calibration",
    "make_demo_dataset",
]

__version__ = "1.0.0"
