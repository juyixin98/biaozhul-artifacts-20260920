"""变换树的异常类型定义。"""


class TransformTreeError(Exception):
    """变换树所有异常的基类。"""


class UnknownFrameError(TransformTreeError):
    """查询的坐标系在树中不存在。"""


class ConnectivityError(TransformTreeError):
    """两个坐标系之间不存在连通路径。"""


class CycleError(TransformTreeError):
    """新增边会在树中形成拓扑环。"""


class MultiParentError(TransformTreeError):
    """同一子坐标系被赋予了多个父坐标系。"""


class ExtrapolationError(TransformTreeError):
    """查询时刻超出某条边时间序列的覆盖范围（禁止外推）。"""
