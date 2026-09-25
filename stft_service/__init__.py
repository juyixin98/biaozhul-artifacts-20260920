"""Streaming Short-Time Fourier Transform (STFT) offline signal-processing service.

Pure backend: numerical in / numerical + file out. No GUI, no audio playback.
"""

from .windows import make_window, WINDOW_NAMES
from .stft import (
    STFTConfig,
    STFTError,
    prepare_config,
    stft,
    istft,
    number_of_frames,
    pad_signal,
    overlap_sums,
    cola_diagnostic,
)
from .stream import StreamingSTFT, StreamingISTFT

__all__ = [
    "make_window",
    "WINDOW_NAMES",
    "STFTConfig",
    "STFTError",
    "prepare_config",
    "stft",
    "istft",
    "number_of_frames",
    "pad_signal",
    "overlap_sums",
    "cola_diagnostic",
    "StreamingSTFT",
    "StreamingISTFT",
]
