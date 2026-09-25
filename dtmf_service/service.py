"""High-level offline service API.

Entry points accept either an in-memory signal, a local PCM file, or a
JSON-style request dict (see ``examples/request_sample.json``) and
return plain dicts — no UI, no audio playback.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import numpy as np

from .detector import DTMFDetector, DetectorConfig
from .pcm import read_pcm
from .synth import synthesize_sequence


def detect_samples(
    samples: np.ndarray,
    sample_rate: int,
    config: DetectorConfig | None = None,
) -> dict[str, Any]:
    """Detect digits in a float sample array. Returns a result dict."""
    detector = DTMFDetector(config)
    return detector.detect(samples, sample_rate).to_dict()


def detect_file(
    path: str | Path,
    sample_rate: int | None = None,
    config: DetectorConfig | None = None,
) -> dict[str, Any]:
    """Detect digits in a local PCM/WAV file. Returns a result dict."""
    samples, rate = read_pcm(path, sample_rate)
    return detect_samples(samples, rate, config)


def handle_request(request: dict[str, Any]) -> dict[str, Any]:
    """Handle one JSON-style request dict.

    Two mutually exclusive input forms:

    * ``{"pcm_file": "path/to/audio.wav"}`` — local PCM file
      (``sample_rate`` required for raw PCM, optional for WAV).
    * ``{"synthesize": {"keys": "159#", "snr_db": 20, ...}}`` — build a
      synthetic signal in-memory and detect on it.

    Optional top-level keys: ``sample_rate`` (default 8000) and
    ``detector`` (dict of DetectorConfig field overrides).
    """
    if not isinstance(request, dict):
        raise TypeError("request must be a dict")

    sample_rate = int(request.get("sample_rate", 8000))
    config = _config_from(request.get("detector"), sample_rate)

    has_file = "pcm_file" in request
    has_synth = "synthesize" in request
    if has_file == has_synth:
        raise ValueError("provide exactly one of 'pcm_file' or 'synthesize'")

    if has_file:
        return detect_file(request["pcm_file"], sample_rate, config)

    synth_args = dict(request["synthesize"])
    keys = synth_args.pop("keys", None)
    if not keys:
        raise ValueError("'synthesize.keys' is required")
    samples = synthesize_sequence(keys, sample_rate, **synth_args)
    return detect_samples(samples, sample_rate, config)


def _config_from(
    overrides: dict[str, Any] | None, sample_rate: int
) -> DetectorConfig | None:
    if not overrides:
        return DetectorConfig(sample_rate=sample_rate)
    unknown = set(overrides) - set(DetectorConfig.__dataclass_fields__)
    if unknown:
        raise ValueError(f"unknown detector config keys: {sorted(unknown)}")
    return DetectorConfig(sample_rate=sample_rate, **overrides)
