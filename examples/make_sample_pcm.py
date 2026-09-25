"""Generate a small sample PCM file (stereo s16le) for the pcm_file job.

Usage:
    python examples/make_sample_pcm.py
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fir_stream import pcm, synth

HERE = os.path.dirname(os.path.abspath(__file__))


def main() -> None:
    x = synth.multi_sine(
        num_channels=2,
        duration_s=0.5,
        sample_rate=48000.0,
        freqs_hz=[330.0, 1200.0, 7000.0],
        seed=17,
    )
    path = os.path.join(HERE, "sample_input.pcm")
    pcm.write_pcm(path, x, "s16le")
    print(f"wrote {path} ({x.shape[0]}ch x {x.shape[1]} samples, s16le)")


if __name__ == "__main__":
    main()
