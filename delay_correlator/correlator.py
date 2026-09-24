"""核心相关计算：FFT 全滞后互相关 + 按重叠长度逐滞后归一化。

符号约定（重要）
----------------
本模块把“延迟”定义为 **B 相对 A 的延迟**：

    B[n] = A[n - d]          （在移位/循环意义上，d 为整数样本）

    d > 0：B 晚于 A（A 先出现，B 后出现），互相关在正滞后 lag=+d 出现峰；
    d < 0：B 早于 A，互相关在负滞后 lag=-|d| 出现峰。

相关器输出的 ``lag`` 即上述 d，符号与该约定一致（测试用例 ``test_negative_delay``
专门校验负号）。

支持两种归一化：
- ``ncc``  ：能量归一化（假设信号均值为 0 时等价于皮尔逊相关系数）；
- ``zncc`` ：局部去均值的归一化互相关（每个滞后在各自重叠区间上减均值），
  对直流偏移更稳健，为默认方法。
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

__all__ = ["CorrelationProfile", "normalized_xcorr"]


@dataclass(frozen=True)
class CorrelationProfile:
    """单个相关剖面的全部数值。

    属性
    ----
    lags:
        滞后轴（样本），范围 ``[-max_lag, max_lag]``，步长 1。
    corr:
        各滞后上的归一化相关系数，约在 ``[-1, 1]``。
    numerator:
        去均值后的逐滞后点积（``zncc`` 下为中心化点积）。
    overlap:
        各滞后的重叠样本数，用于按重叠归一化。
    energy:
        各滞后 A、B 重叠区间的 RMS 能量（去均值后），形状 ``(len(lags), 2)``，
        列顺序为 ``[A, B]``。
    method:
        ``"ncc"`` 或 ``"zncc"``。
    """

    lags: np.ndarray
    corr: np.ndarray
    numerator: np.ndarray
    overlap: np.ndarray
    energy: np.ndarray
    method: str


def xcorr_full(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    """完整线性互相关，采用“B 延迟为正”的约定：

        r[k] = Σ_n a[n] · b[n + k]   （越界下标按零处理）。

    于是 ``B[n] = A[n - d]`` 时 r[d] 最大，峰在正滞后（见模块文档的符号约定）。
    返回数组长度 ``2N - 1``，索引 ``N - 1 + k`` 对应滞后 k。用 FFT 计算，
    补零到不小于 ``2N - 1`` 的 2 的幂以避免循环折叠。
    """
    a = np.asarray(a, dtype=np.float64).ravel()
    b = np.asarray(b, dtype=np.float64).ravel()
    if a.shape != b.shape:
        raise ValueError(f"两通道长度必须相同：len(a)={len(a)}, len(b)={len(b)}")
    n = a.size
    if n == 0:
        return np.zeros(0, dtype=np.float64)
    nfft = 1 << (2 * n - 2).bit_length()  # >= 2N-1 的最小 2 的幂
    # ifft(conj(Fa)·Fb)[k] = Σ_n a[n] b[n+k]，正滞后 k 在数组头部。
    spec = np.conj(np.fft.fft(a, n=nfft)) * np.fft.fft(b, n=nfft)
    full = np.fft.ifft(spec).real
    # 循环相关按滞后 0 在索引 0 排列：负滞后在尾部，正滞后在头部。
    return np.concatenate([full[-(n - 1) :], full[:n]])


def _prefix_stats(x: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """返回前缀和、平方前缀和，以及去均值平方和需要的辅助量。"""
    cs = np.concatenate([[0.0], np.cumsum(x, dtype=np.float64)])
    cs2 = np.concatenate([[0.0], np.cumsum(x * x, dtype=np.float64)])
    return cs, cs2, cs2  # 第三个返回位保持调用对称，实际只用到前两者


def normalized_xcorr(
    a: np.ndarray,
    b: np.ndarray,
    max_lag: int,
    method: str = "zncc",
) -> CorrelationProfile:
    """计算滞后 ``[-max_lag, max_lag]`` 内的归一化互相关。

    参数
    ----
    a, b:
        等长一维实信号。
    max_lag:
        最大允许延迟（样本，含两端）。会被裁剪到不超过 ``N - 1``。
    method:
        ``"zncc"``（默认，局部去均值）或 ``"ncc"``（不去均值）。

    返回
    ----
    CorrelationProfile

    说明
    ----
    对滞后 ``d``，重叠区间为：``d >= 0`` 时 A 的 ``[0, N-d)`` 与 B 的 ``[d, N)``
    配对（验证 B[n]=A[n-d] 在 lag=+d 对齐）；``d < 0`` 时反之。归一化分母为
    两重叠区间（去均值）能量的几何平均，因此相关系数理论上落在 ``[-1, 1]``。
    """
    a = np.asarray(a, dtype=np.float64).ravel()
    b = np.asarray(b, dtype=np.float64).ravel()
    if a.shape != b.shape:
        raise ValueError(f"两通道长度必须相同：len(a)={len(a)}, len(b)={len(b)}")
    if method not in ("ncc", "zncc"):
        raise ValueError(f"未知 method: {method!r}，应为 'ncc' 或 'zncc'")
    n = a.size
    if n < 2:
        raise ValueError("信号长度至少需要 2 个样本")
    max_lag = int(max_lag)
    if max_lag < 0:
        raise ValueError("max_lag 必须非负")
    max_lag = min(max_lag, n - 1)

    lags = np.arange(-max_lag, max_lag + 1, dtype=np.int64)
    full = xcorr_full(a, b)  # 索引 n-1+d 对应滞后 d
    center = n - 1

    cs_a, cs2_a, _ = _prefix_stats(a)
    cs_b, cs2_b, _ = _prefix_stats(b)

    numerator = np.empty(lags.size, dtype=np.float64)
    overlap = np.empty(lags.size, dtype=np.float64)
    energy_a = np.empty(lags.size, dtype=np.float64)
    energy_b = np.empty(lags.size, dtype=np.float64)

    for i, d in enumerate(lags):
        if d >= 0:
            lo_a, hi_a, lo_b, hi_b = 0, n - d, d, n
        else:
            lo_a, hi_a, lo_b, hi_b = -d, n, 0, n + d
        length = hi_a - lo_a  # = hi_b - lo_b
        overlap[i] = length

        sum_a = cs_a[hi_a] - cs_a[lo_a]
        sum_b = cs_b[hi_b] - cs_b[lo_b]
        sum2_a = cs2_a[hi_a] - cs2_a[lo_a]
        sum2_b = cs2_b[hi_b] - cs2_b[lo_b]
        sxy = full[center + d]

        if method == "zncc":
            # 中心化点积：Σxy - Σx·Σy/L
            numerator[i] = sxy - sum_a * sum_b / length
            ea = sum2_a - sum_a * sum_a / length
            eb = sum2_b - sum_b * sum_b / length
        else:
            numerator[i] = sxy
            ea = sum2_a
            eb = sum2_b

        # 浮点误差可能产生极小负值，裁到 0。
        energy_a[i] = ea if ea > 0.0 else 0.0
        energy_b[i] = eb if eb > 0.0 else 0.0

    energy = np.column_stack([energy_a, energy_b])
    denom = np.sqrt(np.maximum(energy_a, 0.0) * np.maximum(energy_b, 0.0))
    with np.errstate(invalid="ignore", divide="ignore"):
        corr = np.where(denom > 0.0, numerator / denom, 0.0)
    # FFT 与求和误差可能使幅度略微超出 1，夹回理论范围。
    corr = np.clip(corr, -1.0, 1.0)

    return CorrelationProfile(
        lags=lags,
        corr=corr,
        numerator=numerator,
        overlap=overlap,
        energy=energy,
        method=method,
    )
