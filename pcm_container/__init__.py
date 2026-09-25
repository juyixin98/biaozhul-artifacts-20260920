"""PCM 容器校验：纯后端离线信号处理与 WAV PCM 子集读写。"""

from .errors import PcmError, WavFormatError, WavTruncatedError, PcmAlignmentError
from .wavio import read_wav, write_wav, WavData, build_wav_bytes, parse_wav
from .amplitude import to_float, from_float, int_min, int_max
from .signals import synthesize
from .service import run_request

__all__ = [
    "PcmError",
    "WavFormatError",
    "WavTruncatedError",
    "PcmAlignmentError",
    "read_wav",
    "write_wav",
    "WavData",
    "build_wav_bytes",
    "parse_wav",
    "to_float",
    "from_float",
    "int_min",
    "int_max",
    "synthesize",
    "run_request",
]

__version__ = "1.0.0"
