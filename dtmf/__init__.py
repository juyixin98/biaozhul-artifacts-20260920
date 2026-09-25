"""双音频率识别（DTMF 风格）纯后端信号处理包。

仅面向合成音频 / 本地 PCM 离线场景，不针对真实电话线路做兼容性承诺。
"""

from .goertzel import goertzel_power, goertzel_power_batch
from .detector import DetectorConfig, DtmfDetector, DecodeResult
from .synth import SynthConfig, synthesize

__all__ = [
    "goertzel_power",
    "goertzel_power_batch",
    "DetectorConfig",
    "DtmfDetector",
    "DecodeResult",
    "SynthConfig",
    "synthesize",
]

__version__ = "0.1.0"
