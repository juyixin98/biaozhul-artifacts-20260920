"""kalman_missing: 支持部分缺测的线性卡尔曼滤波（纯 NumPy 后端）。

公开接口：
- KalmanFilter: 滤波器核心（预测 / 缺测感知更新，Joseph 形式协方差更新）
- run_request: JSON 字典接口（请求 -> 响应）
- KalmanInputError / KalmanNumericalError: 失败状态对应的异常类型
- Tolerances: 数值容差配置
"""

from .exceptions import KalmanError, KalmanInputError, KalmanNumericalError
from .filter import KalmanFilter, StepResult, Tolerances
from .json_interface import run_request

__all__ = [
    "KalmanFilter",
    "StepResult",
    "Tolerances",
    "KalmanError",
    "KalmanInputError",
    "KalmanNumericalError",
    "run_request",
]

__version__ = "0.1.0"
