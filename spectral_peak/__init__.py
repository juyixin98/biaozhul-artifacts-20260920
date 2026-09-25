"""spectral_peak: windowed-FFT peak detection with sub-bin interpolation.

Pure offline signal-processing library. No audio playback, no UI.
"""

from .analyzer import analyze
from .signalgen import tone, multitone

__all__ = ["analyze", "tone", "multitone"]
__version__ = "0.1.0"
