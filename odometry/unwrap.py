"""编码器计数器回绕处理。

计数器为模 M 的环形计数（如 16 位无符号计数器 M=65536）。
相邻两次读数的真实增量通过“折半取模”恢复：

    delta = (raw[i] - raw[i-1] + M/2) mod M - M/2

该公式在 |真实增量| < M/2 时恒成立；若单步真实增量超过 M/2，
则物理上无法与反向回绕区分（欠采样），属于不可恢复情形，
由诊断模块通过速度上限另行标记。
"""

from __future__ import annotations

from typing import Optional, Tuple

import numpy as np


def unwrap_counter_deltas(
    raw_counts: np.ndarray,
    modulus: Optional[int],
) -> Tuple[np.ndarray, np.ndarray]:
    """由原始计数序列计算每步增量，并修正回绕。

    Args:
        raw_counts: 形状 (N,) 的原始计数读数（整数或可安全取整的浮点）。
        modulus: 计数器模值；None 表示计数器不回绕，直接差分。

    Returns:
        (deltas, wrap_flags):
            deltas 形状 (N,)，deltas[0] = 0（首个样本无增量）；
            wrap_flags 形状 (N,) 的布尔数组，标记该步是否发生了回绕修正。
    """
    counts = np.asarray(raw_counts, dtype=np.float64)
    if counts.ndim != 1:
        raise ValueError("raw_counts 必须是一维数组")
    n = counts.shape[0]
    if n == 0:
        return np.zeros(0), np.zeros(0, dtype=bool)

    deltas = np.zeros(n, dtype=np.float64)
    wrap_flags = np.zeros(n, dtype=bool)
    if n == 1:
        return deltas, wrap_flags

    raw_diff = np.diff(counts)
    if modulus is None:
        deltas[1:] = raw_diff
        return deltas, wrap_flags

    half = modulus / 2.0
    corrected = (raw_diff + half) % modulus - half
    # 修正前后不一致即发生了回绕（或超过半模的欠采样，同样标记出来）
    wrap_flags[1:] = corrected != raw_diff
    deltas[1:] = corrected
    return deltas, wrap_flags
