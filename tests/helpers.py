"""测试辅助:手工拼装 WAV 字节,用于制造真实录音机不会产生的畸形容器。"""

from __future__ import annotations

import struct


def raw_chunk(cid: bytes, payload: bytes, *, declared_size: int | None = None,
              pad: bool = True) -> bytes:
    """构造一个 chunk 字节(头 + 负载 + 可选 pad)。

    declared_size 可故意写成与负载长度不符,用于制造截断。
    """
    size = declared_size if declared_size is not None else len(payload)
    out = cid + struct.pack("<I", size) + payload
    if pad and len(payload) & 1:
        out += b"\x00"
    return out


def fmt_payload(channels: int = 1, sample_rate: int = 8000, bits: int = 16,
                tag: int = 0x0001, *, block_align: int | None = None,
                byte_rate: int | None = None, valid_bits: int | None = None) -> bytes:
    """构造 fmt chunk 负载。tag=0xFFFE 时生成 40 字节 extensible 负载。"""
    ba = block_align if block_align is not None else channels * (bits // 8)
    br = byte_rate if byte_rate is not None else sample_rate * ba
    base = struct.pack("<HHIIHH", tag, channels, sample_rate, br, ba, bits)
    if tag == 0xFFFE:
        vb = valid_bits if valid_bits is not None else bits
        guid = bytes.fromhex("0100000000001000800000aa00389b71")
        # cbSize=22, wValidBitsPerSample, dwChannelMask=0, SubFormat GUID
        return base + struct.pack("<HHI", 22, vb, 0) + guid
    return base


def build_wav(chunks: list[bytes] | bytes, *, riff_size: int | None = None,
              magic: bytes = b"RIFF", form: bytes = b"WAVE") -> bytes:
    """把若干 raw_chunk 拼成完整 RIFF 文件。"""
    body = chunks if isinstance(chunks, (bytes, bytearray)) else b"".join(chunks)
    size = riff_size if riff_size is not None else 4 + len(body)
    return magic + struct.pack("<I", size) + form + body


def pcm_frames(*values: int, bits: int = 16) -> bytes:
    """把整数样本打包为小端 PCM(16/24 bit)。"""
    if bits == 16:
        return struct.pack("<" + "h" * len(values), *values)
    out = bytearray()
    for v in values:
        v &= 0xFFFFFF
        out += bytes((v & 0xFF, (v >> 8) & 0xFF, (v >> 16) & 0xFF))
    return bytes(out)
