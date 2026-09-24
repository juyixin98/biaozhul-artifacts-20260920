#!/usr/bin/env python3
"""生成 PCM 输入样例：1 kHz 正弦 + 3 kHz 干扰，fs=8kHz，s16le。"""

from pathlib import Path

import numpy as np

import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fixedpoint_iir.signals import gen_multi_sine, write_pcm

OUT = Path(__file__).resolve().parent.parent / "examples" / "sine_1k_s16.pcm"


def main() -> None:
    x = gen_multi_sine(1024, [1000.0, 3000.0], 8000.0, amplitudes=[0.5, 0.3])
    write_pcm(str(OUT), x, "s16le")
    print(f"已生成 {OUT} ({OUT.stat().st_size} 字节, 1024 样本 s16le)")


if __name__ == "__main__":
    main()
