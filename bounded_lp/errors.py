"""异常类型定义。"""


class LPError(Exception):
    """本包所有自定义异常的基类。"""


class InvalidProblem(LPError):
    """问题数据不合法（维度不一致、NaN、越界等）。"""


class NumericalFailure(LPError):
    """数值失败：迭代超限、疑似循环、矩阵奇异等。"""
