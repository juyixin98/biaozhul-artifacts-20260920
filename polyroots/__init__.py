"""复系数多项式全部根数值求解后端。

模块边界：
- validation: 数值输入与规范化（数据契约入口）
- kernels:   计算内核（伴随矩阵 / Aberth 同时迭代）
- evidence:  残差、因子重构、Vieta、重根/聚类敏感性证据
- reference: mpmath 高精度独立参考（不属于被测内核）
- ordering:  稳定排序与显式容差的共轭配对
- engine:    编排与状态契约
- runlog:    可重放运行日志
"""

from .errors import (
    ComputationFailedError,
    ConflictingStateError,
    InvalidCoefficientError,
    InvalidOptionError,
    NonFiniteCoefficientError,
    PolyrootError,
    ResourceExhaustedError,
    ZeroPolynomialError,
)
from .engine import solve_polynomial
from .models import SolveOptions, SolveResult, SolveStatus

__all__ = [
    "solve_polynomial",
    "SolveOptions",
    "SolveResult",
    "SolveStatus",
    "PolyrootError",
    "InvalidCoefficientError",
    "NonFiniteCoefficientError",
    "ZeroPolynomialError",
    "InvalidOptionError",
    "ConflictingStateError",
    "ResourceExhaustedError",
    "ComputationFailedError",
]
