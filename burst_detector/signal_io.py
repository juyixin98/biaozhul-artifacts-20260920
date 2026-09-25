"""信号输入输出：合成信号生成、缺样注入、原始 PCM 文件读写。

所有合成函数均返回 *float64* 的新数组（不修改入参），PCM 读写支持
常见的交错整型/浮点格式，单声道或多声道可按通道选择/平均。
"""

from __future__ import annotations

import numpy as np

PCM_DTYPES = {
    "int16": (np.int16, -32768.0, 32767.0),
    "int32": (np.int32, -2147483648.0, 2147483647.0),
    "uint8": (np.uint8, 0.0, 255.0),
    "float32": (np.float32, None, None),
    "float64": (np.float64, None, None),
}


# ---------------------------------------------------------------- 合成信号
def gaussian_noise(
    n: int, *, sigma: float = 1.0, mean: float = 0.0, seed: int | None = 0
) -> np.ndarray:
    """生成高斯白噪声（确定性 seed 便于复现实验）。"""
    rng = np.random.default_rng(seed)
    return mean + sigma * rng.standard_normal(int(n))


def add_step(
    x: np.ndarray, *, at: int, amplitude: float
) -> np.ndarray:
    """在样本索引 ``at`` 处叠加持续到结尾的阶跃，返回新数组。"""
    y = np.array(x, dtype=np.float64, copy=True)
    if 0 <= at < len(y):
        y[at:] = y[at:] + amplitude
    return y


def add_drift(
    x: np.ndarray, *, at: int = 0, rate: float = 0.001
) -> np.ndarray:
    """从 ``at`` 起叠加线性漂移 rate*(i-at)，返回新数组。"""
    y = np.array(x, dtype=np.float64, copy=True)
    idx = np.arange(len(y), dtype=np.float64) - at
    y = y + np.where(idx >= 0, rate * idx, 0.0)
    return y


def add_spikes(
    x: np.ndarray, positions, amplitudes=8.0
) -> np.ndarray:
    """在指定位置叠加孤立尖峰，``amplitudes`` 可为标量或等长序列。"""
    y = np.array(x, dtype=np.float64, copy=True)
    positions = np.asarray(positions, dtype=int)
    amps = np.broadcast_to(np.asarray(amplitudes, dtype=np.float64), positions.shape)
    for p, a in zip(positions, amps):
        if 0 <= p < len(y):
            y[p] += a
    return y


def inject_missing(
    x: np.ndarray, positions, *, fill: float = np.nan
) -> np.ndarray:
    """把指定位置替换为缺样标记（默认 NaN），返回新数组。"""
    y = np.array(x, dtype=np.float64, copy=True)
    for p in positions:
        if 0 <= p < len(y):
            y[p] = fill
    return y


def make_scenario(
    kind: str,
    *,
    n: int = 2000,
    sigma: float = 1.0,
    seed: int = 0,
    step_at: int = 1000,
    step_amp: float = 6.0,
    drift_at: int = 800,
    drift_rate: float = 0.01,
    spike_positions=(600, 1200, 1700),
    spike_amp: float = 8.0,
) -> np.ndarray:
    """生成验收用标准场景：``step`` / ``drift`` / ``spike`` / ``clean``。

    尖峰幅度以噪声标准差的倍数给出（内部乘 sigma），阶跃同理。
    """
    base = gaussian_noise(n, sigma=sigma, seed=seed)
    if kind == "clean":
        return base
    if kind == "step":
        return add_step(base, at=step_at, amplitude=step_amp * sigma)
    if kind == "drift":
        return add_drift(base, at=drift_at, rate=drift_rate * sigma)
    if kind == "spike":
        return add_spikes(
            base, positions=spike_positions, amplitudes=spike_amp * sigma
        )
    raise ValueError(f"未知场景 {kind!r}，可选: clean/step/drift/spike")


# ---------------------------------------------------------------- PCM 读写
def read_pcm(
    path: str,
    *,
    dtype: str = "int16",
    channels: int = 1,
    channel: int | str = 0,
    big_endian: bool = False,
    normalize: bool = True,
) -> np.ndarray:
    """读取裸 PCM 文件为 float64 一维信号。

    参数
    ----
    dtype: ``int16/int32/uint8/float32/float64``
    channels: 交错存储的声道数
    channel: 取哪个声道（整数索引），或 ``"avg"`` 对各声道求平均
    normalize: 整型 PCM 是否按满量程归一化到 ±1；浮点格式忽略此项
    """
    if dtype not in PCM_DTYPES:
        raise ValueError(f"不支持的 PCM dtype {dtype!r}，可选 {sorted(PCM_DTYPES)}")
    np_dtype, _, _ = PCM_DTYPES[dtype]
    raw = np.fromfile(path, dtype=np.dtype(np_dtype).newbyteorder(
        ">" if big_endian else "<"
    ))
    if raw.size % channels != 0:
        raise ValueError(
            f"PCM 样本数 {raw.size} 不能被声道数 {channels} 整除，参数可能有误"
        )
    frames = raw.reshape(-1, channels).astype(np.float64)
    if channel == "avg":
        sig = frames.mean(axis=1)
    else:
        ch = int(channel)
        if not 0 <= ch < channels:
            raise ValueError(f"声道 {ch} 超出范围 [0, {channels - 1}]")
        sig = frames[:, ch]
    if normalize and np_dtype in (np.int16, np.int32, np.uint8):
        full = max(abs(PCM_DTYPES[dtype][1]), abs(PCM_DTYPES[dtype][2]))
        if dtype == "uint8":  # uint8 PCM 约定中心 128
            sig = (sig - 128.0) / 128.0
        else:
            sig = sig / full
    return sig


def write_pcm(
    path: str,
    signal: np.ndarray,
    *,
    dtype: str = "int16",
    big_endian: bool = False,
) -> int:
    """把一维实信号量化写为裸 PCM，返回写入样本数。"""
    if dtype not in PCM_DTYPES:
        raise ValueError(f"不支持的 PCM dtype {dtype!r}")
    np_dtype, lo, hi = PCM_DTYPES[dtype]
    x = np.asarray(signal, dtype=np.float64)
    if not np.all(np.isfinite(x)):
        raise ValueError("写入 PCM 前信号必须全部有限（请先处理缺样）")
    out_dtype = np.dtype(np_dtype).newbyteorder(">" if big_endian else "<")
    if np.issubdtype(np_dtype, np.integer):
        if dtype == "uint8":
            q = np.round(x * 128.0 + 128.0)
        else:
            full = max(abs(lo), abs(hi))
            q = np.round(x * full)
        q = np.clip(q, lo, hi).astype(out_dtype)
    else:
        q = x.astype(out_dtype)
    q.tofile(path)
    return int(q.size)
