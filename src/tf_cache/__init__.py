"""时间缓存 TF 树查询服务（纯合成数据 / 离线回放，无硬件接入）。

模块组成：
- errors:   领域异常
- se3:      SE3 刚体变换（平移 + 单位四元数），乘法 / 逆 / 插值（SLERP）
- tree:     带时间戳的 TF 树：TimeCache（单边）+ TransformTree（多边组合查询）
- app:      FastAPI HTTP 服务
"""

from .errors import (
    ExtrapolationNotAllowedError,
    FrameNotFoundError,
    InvalidTransformError,
    TFCycleError,
    TFError,
)
from .se3 import SE3Transform
from .tree import TimeCache, TransformTree

__all__ = [
    "SE3Transform",
    "TimeCache",
    "TransformTree",
    "TFError",
    "TFCycleError",
    "FrameNotFoundError",
    "ExtrapolationNotAllowedError",
    "InvalidTransformError",
]
