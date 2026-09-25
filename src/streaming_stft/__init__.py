"""Streaming Short-Time Fourier Transform (offline, NumPy-only).

Pure back-end signal-processing service:

* analysis/synthesis (:mod:`streaming_stft.stft`)
* chunked online framing and overlap-add (:mod:`streaming_stft.streaming`)
* synthetic / raw PCM input, numeric + file output only
  (:mod:`streaming_stft.service`, :mod:`streaming_stft.cli`)
"""

from .stft import (
    ISTFTOutput,
    STFTConfig,
    STFTOutput,
    istft,
    stft,
    window_diagnostic,
)
from .streaming import ISTFTStreamer, STFTStreamer

__all__ = [
    "STFTConfig",
    "STFTOutput",
    "ISTFTOutput",
    "stft",
    "istft",
    "window_diagnostic",
    "STFTStreamer",
    "ISTFTStreamer",
]

__version__ = "0.1.0"
