"""Offline dual-tone (DTMF-style) frequency detection service.

Pure backend library: numeric input (NumPy arrays or local PCM files),
numeric/JSON output. No player, no UI.
"""

from .detector import DetectorConfig, DetectionResult, DigitEvent, DTMFDetector
from .goertzel import goertzel_power
from .synth import KEYPAD, synthesize_sequence, synthesize_tone

__all__ = [
    "DetectorConfig",
    "DetectionResult",
    "DigitEvent",
    "DTMFDetector",
    "KEYPAD",
    "goertzel_power",
    "synthesize_sequence",
    "synthesize_tone",
]

__version__ = "0.1.0"
