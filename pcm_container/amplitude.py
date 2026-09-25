"""整数 PCM 样本与 [-1.0, 1.0) 浮点振幅之间的转换。

约定（与主流 DAW / WAVE 规范一致）：
- 正满量程 int_max 映射为 +1.0；
- 负满量程 int_min 映射为 -1.0，因此正负刻度不对称（16 位负方向多一个码）；
- 浮点 -> 整数采用"半远离零"舍入（np.rint，银行家舍入的对称等价写法在此值域
  与四舍五入一致），然后裁剪到 [int_min, int_max]。

float -> int -> float 在已裁剪的整数域上是精确恒等（除法因子为 2^n，
浮点表示精确）；int -> float -> int 亦为恒等（最近整数舍入后恰为原码）。
"""

from __future__ import annotations

import numpy as np

SUPPORTED_BITS = (16, 24)


def int_min(bits: int) -> int:
    """指定位深的最小有符号整数码，例如 16 位 -> -32768。"""
    if bits not in SUPPORTED_BITS:
        raise ValueError(f"unsupported bit depth: {bits} (only {SUPPORTED_BITS})")
    return -(1 << (bits - 1))


def int_max(bits: int) -> int:
    """指定位深的最大有符号整数码，例如 16 位 -> 32767。"""
    if bits not in SUPPORTED_BITS:
        raise ValueError(f"unsupported bit depth: {bits} (only {SUPPORTED_BITS})")
    return (1 << (bits - 1)) - 1


def to_float(samples: np.ndarray, bits: int) -> np.ndarray:
    """有符号整数样本 -> float64 振幅。

    samples 可以是任意形状（多声道时常见形状为 (n_frames, channels)）。
    输出形状与输入一致，范围在 [-1.0, 1.0]（正方向实际到不了 +1.0 以上，
    int_max/2^(n-1) 略小于 1；为保持对称可读，这里仍称满量程为 +/-1.0）。
    """
    if bits not in SUPPORTED_BITS:
        raise ValueError(f"unsupported bit depth: {bits} (only {SUPPORTED_BITS})")
    arr = np.asarray(samples)
    scale = np.float64(1 << (bits - 1))
    out = arr.astype(np.float64, copy=True) / scale
    # 数值上 int_max/scale = 1 - 1/scale；文档口径称 +1.0 为满量程刻度。
    return out


def from_float(amplitudes: np.ndarray, bits: int) -> np.ndarray:
    """float 振幅 -> 有符号 int32 整数样本（裁剪 + 最近值舍入）。

    返回 int32 是为了让 16 位与 24 位共用同一管线；写盘时再按位深打包。
    非有限值（NaN/Inf）会先映射到 0（NaN）或 +/-满量程（Inf），避免产生
    未定义码——NaN 比较全部为 False，np.clip 对它无效，故显式处理。
    """
    if bits not in SUPPORTED_BITS:
        raise ValueError(f"unsupported bit depth: {bits} (only {SUPPORTED_BITS})")
    arr = np.asarray(amplitudes, dtype=np.float64)
    arr = np.where(np.isnan(arr), 0.0, arr)
    scale = np.float64(1 << (bits - 1))
    rounded = np.rint(arr * scale)
    clipped = np.clip(rounded, int_min(bits), int_max(bits))
    return clipped.astype(np.int32)
