"""生成示例 PCM 输入文件：examples/data/two_tone_48k.s16le。

用法：python examples/make_example_input.py
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from rational_resampler.pcm_io import write_pcm
from rational_resampler.signals import multitone

HERE = os.path.dirname(os.path.abspath(__file__))


def main():
    os.makedirs(os.path.join(HERE, "data"), exist_ok=True)
    x = multitone([1000.0, 5000.0], fs=48000.0, duration=0.1, amplitude=0.8)
    path = os.path.join(HERE, "data", "two_tone_48k.s16le")
    write_pcm(path, x, "s16le")
    print(f"wrote {path}: {x.size} samples @ 48000 Hz, s16le")


if __name__ == "__main__":
    main()
