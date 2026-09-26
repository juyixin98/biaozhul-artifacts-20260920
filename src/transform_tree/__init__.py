"""坐标变换时间树（仅依赖 NumPy 的纯后端库）。"""

from .errors import (
    CycleDetectedError,
    DuplicateTimestampError,
    FramesNotConnectedError,
    InvalidKeyframeError,
    InvalidRequestError,
    InvalidTransformError,
    MultipleParentsError,
    TimeGapError,
    TimeNotCoveredError,
    TransformTreeError,
    UnknownFrameError,
)
from .timed_sequence import Keyframe, StaticTransformProvider, TimedTransformSequence
from .transform import Transform
from .tree import Edge, TransformTree

__all__ = [
    "Transform",
    "TransformTree",
    "Edge",
    "Keyframe",
    "TimedTransformSequence",
    "StaticTransformProvider",
    "TransformTreeError",
    "UnknownFrameError",
    "MultipleParentsError",
    "CycleDetectedError",
    "FramesNotConnectedError",
    "DuplicateTimestampError",
    "TimeNotCoveredError",
    "TimeGapError",
    "InvalidTransformError",
    "InvalidKeyframeError",
    "InvalidRequestError",
]

__version__ = "0.1.0"
