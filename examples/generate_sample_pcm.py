"""Generate examples/sample_f32.pcm: a deterministic chirp plus harmonics.

Run once before using examples/request_pcm.json::

    PYTHONPATH=src python examples/generate_sample_pcm.py
"""

from __future__ import annotations

from pathlib import Path

import numpy as np

from streaming_stft.pcmio import write_pcm
from streaming_stft.signals import SyntheticSpec, generate_signal


def main() -> None:
    spec = SyntheticSpec(
        duration_s=0.1,
        sample_rate=8000,
        frequencies=(300.0, 900.0),
        amplitudes=(0.5, 0.2),
        chirp_to=1500.0,
        noise_std=0.005,
        seed=7,
    )
    signal = generate_signal(spec)
    out = Path(__file__).with_name("sample_f32.pcm")
    write_pcm(out, signal, dtype="float32")
    print(f"wrote {out} ({signal.shape[0]} samples, float32 LE)")


if __name__ == "__main__":
    main()
