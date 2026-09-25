"""Streaming FIR state management: offline multi-channel FIR convolution
with runtime filter switching and explicit crossfade."""

from .core import MultiChannelFIR, StreamingFIR
from .analysis import boundary_jumps, find_discontinuities, max_abs_error

__all__ = [
    "StreamingFIR",
    "MultiChannelFIR",
    "boundary_jumps",
    "find_discontinuities",
    "max_abs_error",
]

__version__ = "0.1.0"
