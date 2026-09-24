"""整数 PCM 样本与浮点振幅之间的转换。

约定(全项目统一):
- 浮点振幅域为 [-1.0, 1.0),即 full-scale 负值映射为 -1.0,
  full-scale 正值映射为 1 - 2**-(bits-1)(int16 时为 32767/32768)。
- 反量化:  float = int / 2**(bits-1)
- 量化:    int   = round(float * 2**(bits-1)),越界裁剪到整数域。
- 24 bit 样本以小端 3 字节存储,符号位在最高字节的 bit 7。
"""

from __future__ import annotations

import numpy as np

SUPPORTED_BIT_DEPTHS = (16, 24)

_PCM_DTYPES = {16: np.int16, 24: np.int32}


class UnsupportedBitDepthError(ValueError):
    """请求了不支持的位深。"""


def int_dtype(bits: int) -> np.dtype:
    """返回该位深在内存中使用的 NumPy dtype(24 bit 用 int32 承载)。"""
    try:
        return np.dtype(_PCM_DTYPES[bits])
    except KeyError:
        raise UnsupportedBitDepthError(
            f"unsupported bit depth {bits}; supported: {SUPPORTED_BIT_DEPTHS}"
        ) from None


def int_range(bits: int) -> tuple[int, int]:
    """返回该位深的有符号整数域 [min, max]。"""
    int_dtype(bits)  # 校验位深
    half = 1 << (bits - 1)
    return -half, half - 1


def decode_pcm16(data: bytes) -> np.ndarray:
    """小端 int16 字节流 -> int16 一维数组(不区分声道,交错保持原序)。"""
    if len(data) % 2 != 0:
        raise ValueError(
            f"16-bit PCM byte stream length {len(data)} is not a multiple of 2"
        )
    return np.frombuffer(data, dtype="<i2").astype(np.int16)


def decode_pcm24(data: bytes) -> np.ndarray:
    """小端 24 bit(3 字节)字节流 -> int32 一维数组,含符号扩展。"""
    if len(data) % 3 != 0:
        raise ValueError(
            f"24-bit PCM byte stream length {len(data)} is not a multiple of 3"
        )
    b = np.frombuffer(data, dtype=np.uint8).reshape(-1, 3).astype(np.int32)
    values = b[:, 0] | (b[:, 1] << 8) | (b[:, 2] << 16)
    # 符号扩展:bit 23 为符号位
    values = np.where(values & 0x800000, values - (1 << 24), values)
    return values


def decode_pcm(data: bytes, bits: int) -> np.ndarray:
    """按位深解码原始 PCM 字节流为整数样本数组。"""
    if bits == 16:
        return decode_pcm16(data)
    if bits == 24:
        return decode_pcm24(data)
    raise UnsupportedBitDepthError(
        f"unsupported bit depth {bits}; supported: {SUPPORTED_BIT_DEPTHS}"
    )


def encode_pcm16(samples: np.ndarray) -> bytes:
    """int16 样本数组 -> 小端字节流。"""
    arr = np.asarray(samples)
    if arr.dtype != np.int16:
        arr = arr.astype(np.int16)
    return arr.astype("<i2", copy=False).tobytes()


def encode_pcm24(samples: np.ndarray) -> bytes:
    """int32 样本数组(值域限 24 bit)-> 小端 3 字节交错字节流。"""
    arr = np.asarray(samples)
    lo, hi = int_range(24)
    if arr.size and (arr.min() < lo or arr.max() > hi):
        raise ValueError(f"sample out of 24-bit range [{lo}, {hi}]")
    v = arr.astype(np.int32) & 0xFFFFFF
    out = np.empty((v.size, 3), dtype=np.uint8)
    out[:, 0] = v & 0xFF
    out[:, 1] = (v >> 8) & 0xFF
    out[:, 2] = (v >> 16) & 0xFF
    return out.tobytes()


def encode_pcm(samples: np.ndarray, bits: int) -> bytes:
    """按位深把整数样本数组编码为原始 PCM 字节流。"""
    if bits == 16:
        return encode_pcm16(samples)
    if bits == 24:
        return encode_pcm24(samples)
    raise UnsupportedBitDepthError(
        f"unsupported bit depth {bits}; supported: {SUPPORTED_BIT_DEPTHS}"
    )


def dequantize(samples: np.ndarray, bits: int) -> np.ndarray:
    """整数样本 -> 浮点振幅 [-1.0, 1.0)。"""
    int_dtype(bits)  # 校验位深
    return np.asarray(samples, dtype=np.float64) / float(1 << (bits - 1))


def quantize(floats: np.ndarray, bits: int) -> np.ndarray:
    """浮点振幅 -> 整数样本(四舍五入,越界裁剪;拒绝 NaN/Inf)。"""
    dtype = int_dtype(bits)
    arr = np.asarray(floats, dtype=np.float64)
    if not np.all(np.isfinite(arr)):
        raise ValueError("float samples must be finite (no NaN/Inf)")
    lo, hi = int_range(bits)
    scaled = np.rint(arr * float(1 << (bits - 1)))
    return np.clip(scaled, lo, hi).astype(dtype)
