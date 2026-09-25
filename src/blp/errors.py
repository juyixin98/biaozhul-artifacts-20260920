"""自定义异常类型。

调用方（CLI / JSON 接口层）捕获 :class:`LPInputError` 后返回
``status="invalid_input"``；其余异常视为求解器内部失败。
"""

from __future__ import annotations


class LPError(Exception):
    """本库所有异常的基类。"""


class LPInputError(LPError):
    """输入问题描述不合法（维度不一致、取值越界、未知符号等）。"""


class LPNumericalError(LPError):
    """数值层面无法可靠求解（奇异基、枢轴过小、残差核验失败等）。"""


class LPLimitError(LPError):
    """达到迭代/问题规模上限，未能给出结论。"""
