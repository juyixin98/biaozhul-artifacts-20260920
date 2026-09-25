"""突发异常信号检测（纯后端，NumPy 实现）。

仅依赖 NumPy 的离线信号处理服务：基于滑动稳健统计（中位数 + MAD）的
因果脉冲异常检测，阈值只依赖过去样本，不使用任何未来信息。
"""

from .detector import (
    BurstAnomalyDetector,
    Decision,
    DetectorConfig,
    PointResult,
)
from .processing import DetectionResult, detect_blocks, detect_signal, run_detector
from .robust_stats import StreamingRobustStats
from .signal_io import (
    PCM_DTYPES,
    add_drift,
    add_spikes,
    add_step,
    gaussian_noise,
    inject_missing,
    make_scenario,
    read_pcm,
    write_pcm,
)
from .evaluation import EvalReport, LabeledEvent, evaluate

__all__ = [
    "BurstAnomalyDetector",
    "Decision",
    "DetectorConfig",
    "PointResult",
    "DetectionResult",
    "detect_signal",
    "detect_blocks",
    "run_detector",
    "StreamingRobustStats",
    "PCM_DTYPES",
    "gaussian_noise",
    "add_step",
    "add_drift",
    "add_spikes",
    "inject_missing",
    "make_scenario",
    "read_pcm",
    "write_pcm",
    "LabeledEvent",
    "EvalReport",
    "evaluate",
]

__version__ = "0.1.0"
