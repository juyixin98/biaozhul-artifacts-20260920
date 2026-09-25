#!/usr/bin/env python3
"""生成验收用的边界/畸形 WAV 文件（全部为合成字节，无需真实录音）。

在 out/edge_cases/ 下产生：
- extremal_16.wav / extremal_24.wav   极值码样本（含 -满量程/0/+满量程）
- unknown_chunks.wav                 含奇数长度未知 chunk（验跳过与 pad）
- truncated_chunk.wav                chunk 体被截断（应拒绝 WavTruncatedError）
- bad_alignment.wav                  data 长度不是 block align 整数倍
- float32.wav                        IEEE float PCM（应拒绝 WavFormatError）
- pcm8.wav                           8 位 PCM（应拒绝 WavFormatError）
- riff_size_wrong.wav                RIFF 长度被篡改（可读但标记不匹配）

用法：python examples/generate_edge_cases.py
"""

import struct
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from pcm_container import wavio  # noqa: E402

OUT = Path("out/edge_cases")


def chunk(cid: bytes, body: bytes, pad: bool = True) -> bytes:
    out = cid + struct.pack("<I", len(body)) + body
    if len(body) & 1 and pad:
        out += b"\x00"
    return out


def riff(chunks: bytes) -> bytes:
    return b"RIFF" + struct.pack("<I", len(chunks) + 4) + b"WAVE" + chunks


def fmt(channels=1, rate=48000, bits=16, tag=1):
    block = channels * bits // 8
    return struct.pack("<HHIIHH", tag, channels, rate, rate * block, block, bits)


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)

    # 1. 极值样本 ----------------------------------------------------------
    for bits in (16, 24):
        imax = (1 << (bits - 1)) - 1
        imin = -(1 << (bits - 1))
        codes = np.array([imin, imin, 0, imax, imax], dtype=np.int32)
        wavio.write_wav(str(OUT / f"extremal_{bits}.wav"), codes, 48000, 1, bits)

    # 2. 未知 chunk：偶数长度 JUNK + 奇数长度 labl（必须补 pad 才能对齐） ---
    data = np.zeros(8, dtype=np.int16).tobytes()
    good = (
        chunk(b"fmt ", fmt())
        + chunk(b"JUNK", b"\xAB" * 10)
        + chunk(b"labl", b"xy")      # 2 字节偶数
        + chunk(b"note", b"z")      # 1 字节奇数 -> pad
        + chunk(b"data", data)
    )
    (OUT / "unknown_chunks.wav").write_bytes(riff(good))

    # 3. 截断 chunk：未知 chunk 声明 12 字节，只给 4 字节 --------------------
    trunc = (
        chunk(b"fmt ", fmt())
        + b"JUNK" + struct.pack("<I", 12) + b"\x00" * 4
    )
    (OUT / "truncated_chunk.wav").write_bytes(riff(trunc))

    # 4. 错误采样对齐：16 位单声道，data 长度 9 字节（奇数） ----------------
    mis = chunk(b"fmt ", fmt()) + chunk(b"data", b"\x00" * 9)
    (OUT / "bad_alignment.wav").write_bytes(riff(mis))

    # 5. IEEE float 32 位（wFormatTag=3） ----------------------------------
    f32 = chunk(b"fmt ", fmt(bits=32, tag=3)) + chunk(b"data", b"\x00" * 32)
    (OUT / "float32.wav").write_bytes(riff(f32))

    # 6. 8 位整数 PCM ------------------------------------------------------
    p8 = chunk(b"fmt ", fmt(bits=8)) + chunk(b"data", b"\x80" * 16)
    (OUT / "pcm8.wav").write_bytes(riff(p8))

    # 7. RIFF 长度错误：合法内容但声明尺寸 +7 ------------------------------
    blob = bytearray(wavio.build_wav_bytes(np.arange(6, dtype=np.int32), 48000, 1, 16))
    struct.pack_into("<I", blob, 4, struct.unpack_from("<I", blob, 4)[0] + 7)
    (OUT / "riff_size_wrong.wav").write_bytes(bytes(blob))

    print("已生成边界文件到", OUT)
    for p in sorted(OUT.iterdir()):
        print(f"  {p.name:24s} {p.stat().st_size} 字节")


if __name__ == "__main__":
    main()
