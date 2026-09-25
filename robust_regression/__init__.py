"""鲁棒线性回归纯后端计算库（仅依赖 NumPy）。

公开接口：
- fit_huber: Huber 损失 IRLS（迭代重加权最小二乘）拟合
- fit_ols:   普通最小二乘（SVD 最小范数解），用于对照
- fit_from_json: JSON 字典 -> JSON 可序列化响应字典
- InputError / NumericalError: 输入与数值错误
"""

from .huber import fit_huber, fit_ols, huber_objective, huber_weights
from .validation import InputError, NumericalError
from .api import fit_from_json

__all__ = [
    "fit_huber",
    "fit_ols",
    "huber_objective",
    "huber_weights",
    "fit_from_json",
    "InputError",
    "NumericalError",
]

__version__ = "1.0.0"
