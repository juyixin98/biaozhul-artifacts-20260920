#!/usr/bin/env python3
"""生成演示用的"问题容器" WAV(合成字节,无需真实录音)。

输出到 examples/output/:
- fixture_junk_odd16.wav              奇数长度 JUNK chunk(验证跳过+pad)
- fixture_truncated_junk.wav          JUNK 声明长度超过文件末尾(截断)
- fixture_partial_frame24stereo.wav   data 不是帧对齐(24bit 立体声)
- fixture_8bit.wav                    8-bit PCM(必须拒绝)
- fixture_float32.wav                IEEE float(必须拒绝)
- fixture_riff_trailing.wav           RIFF 长度与文件不符 + 尾部垃圾(告警)
"""

from __future__ import annotations

import os
import struct
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.helpers import build_wav, fmt_payload, pcm_frames, raw_chunk  # noqa: E402

OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "output")


def main() -> int:
    os.makedirs(OUT, exist_ok=True)
    written: list[str] = []

    def save(name: str, blob: bytes) -> None:
        path = os.path.join(OUT, name)
        with open(path, "wb") as fh:
            fh.write(blob)
        written.append(f"{name} ({len(blob)} bytes)")

    save("fixture_junk_odd16.wav", build_wav([
        raw_chunk(b"JUNK", b"\x00" * 13),  # 13 字节负载 + 1 pad
        raw_chunk(b"fmt ", fmt_payload(bits=16, channels=1, sample_rate=8000)),
        raw_chunk(b"LIST", b"ab"),         # 奇数负载 + pad
        raw_chunk(b"data", pcm_frames(-32768, 32767, 1234, -1234)),
    ]))

    save("fixture_truncated_junk.wav", build_wav([
        raw_chunk(b"JUNK", b"\x00" * 4, declared_size=64),  # 声明 64,实际 4
        raw_chunk(b"fmt ", fmt_payload()),
        raw_chunk(b"data", pcm_frames(1)),
    ]))

    save("fixture_partial_frame24stereo.wav", build_wav([
        raw_chunk(b"fmt ", fmt_payload(bits=24, channels=2, sample_rate=48000)),
        raw_chunk(b"data", pcm_frames(-8388608, 8388607, bits=24) + b"\xAA\xBB"),
    ]))

    save("fixture_8bit.wav", build_wav([
        raw_chunk(b"fmt ", fmt_payload(bits=8, channels=1, sample_rate=8000)),
        raw_chunk(b"data", b"\x00" * 16),
    ]))

    save("fixture_float32.wav", build_wav([
        raw_chunk(b"fmt ", fmt_payload(bits=32, channels=1, sample_rate=48000,
                                       tag=0x0003)),
        raw_chunk(b"data", b"\x00" * 16),
    ]))

    base = build_wav([
        raw_chunk(b"fmt ", fmt_payload(bits=16, channels=1, sample_rate=8000)),
        raw_chunk(b"data", pcm_frames(1, 2)),
    ])
    save("fixture_riff_trailing.wav", base + b"\xDE\xAD\xBE")

    for line in written:
        print(line)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
