"""变换树相关异常类型。"""


class TransformTreeError(Exception):
    """所有变换树异常的基类。"""


class UnknownFrameError(TransformTreeError):
    """查询或引用了未注册的坐标系。"""


class MultipleParentsError(TransformTreeError):
    """同一个子坐标系被挂到了第二个父坐标系下（多父冲突）。"""


class CycleDetectedError(TransformTreeError):
    """新增边会使坐标帧图产生环。"""


class FramesNotConnectedError(TransformTreeError):
    """两个坐标系属于不同的根，之间不存在变换路径。"""


class DuplicateTimestampError(TransformTreeError):
    """同一条时间序列中出现了重复时间戳。"""


class InvalidKeyframeError(TransformTreeError):
    """关键帧内容非法（非有限时间、空序列等）。"""


class TimeNotCoveredError(TransformTreeError):
    """查询时刻落在某条边时间序列的覆盖范围之外（不允许外推）。"""


class TimeGapError(TransformTreeError):
    """查询时刻两侧关键帧间隔超过允许的最大插值缺口。"""


class InvalidTransformError(TransformTreeError):
    """旋转矩阵 / 四元数 / 平移向量不合法。"""


class InvalidRequestError(TransformTreeError):
    """JSON 请求结构或字段不合法。"""
