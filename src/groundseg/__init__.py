"""点云地面分割后端（纯 NumPy + FastAPI）。

公开入口：
    segment_points  —— 分块 RANSAC 地面分割
    GroundSegParams —— 参数集合
    SegmentResult   —— 结果集合
    evaluate        —— 与人工真值对比，计算精确率/召回率
"""

from .params import GroundSegParams
from .segment import SegmentResult, segment_points
from .metrics import evaluate, Metrics

__all__ = [
    "GroundSegParams",
    "SegmentResult",
    "segment_points",
    "evaluate",
    "Metrics",
]

__version__ = "1.0.0"
