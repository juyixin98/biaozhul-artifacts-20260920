"""TF 服务的领域异常。"""


class TFError(Exception):
    """所有 TF 相关错误的基类。"""


class InvalidTransformError(TFError):
    """四元数非单位、参数形状错误等非法输入。"""


class TFCycleError(TFError):
    """向树中添加边会形成环。"""


class FrameNotFoundError(TFError):
    """引用了不存在的坐标系，或两坐标系之间没有连通链。"""


class ExtrapolationNotAllowedError(TFError):
    """查询时间落在时间戳覆盖范围之外（拒绝外推）。"""


class DuplicateTimestampError(TFError):
    """同一对坐标系在同一时刻写入了不一致（或重复）的采样。"""
