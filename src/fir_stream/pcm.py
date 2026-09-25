"""本地 PCM(raw,无文件头)读写。

布局:样本按帧交错(interleaved),每帧 channels 个样本。
读入返回 (n_frames, channels) 的 float64,整型 PCM 归一化到 [-1, 1);
写出时按位宽量化和裁剪。
"""

from __future__ import annotations

import os

import numpy as np

_INT_DTYPE = {
    16: np.dtype("<i2"),
    24: None,  # 特殊处理
    32: np.dtype("<i4"),
}


def read_pcm(path: str, channels: int = 1, dtype: str = "s16le") -> np.ndarray:
    """读取 raw PCM 文件,返回 (n, channels) float64。"""
    raw = np.fromfile(path, dtype=np.uint8)
    if dtype == "s16le":
        a = raw.view("<i2")
        x = a.astype(np.float64) / 32768.0
    elif dtype == "s24le":
        if raw.size % 3 != 0:
            raise ValueError("s24le 文件大小不是 3 的倍数")
        b = raw.reshape(-1, 3).astype(np.int32)
        a = b[:, 0] | (b[:, 1] << 8) | (b[:, 2] << 16)
        a = np.where(a >= 1 << 23, a - (1 << 24), a)
        x = a.astype(np.float64) / float(1 << 23)
    elif dtype == "s32le":
        a = raw.view("<i4")
        x = a.astype(np.float64) / float(1 << 31)
    elif dtype == "f32le":
        x = raw.view("<f4").astype(np.float64)
    elif dtype == "f64le":
        x = raw.view("<f8").astype(np.float64)
    else:
        raise ValueError(f"不支持的 PCM 格式: {dtype!r}")

    channels = int(channels)
    if channels > 1:
        if x.size % channels != 0:
            raise ValueError(
                f"样本数 {x.size} 不是通道数 {channels} 的整数倍"
            )
        x = x.reshape(-1, channels)
    return np.ascontiguousarray(x)


def write_pcm(path: str, x: np.ndarray, dtype: str = "s16le") -> None:
    """把 (n,) 或 (n, channels) 的浮点信号写成 raw PCM。"""
    parent = os.path.dirname(os.path.abspath(path))
    os.makedirs(parent, exist_ok=True)
    arr = np.asarray(x, dtype=np.float64)
    flat = arr.ravel()  # (n, C) 行优先展开即帧交错
    if dtype == "s16le":
        q = np.clip(np.round(flat * 32768.0), -32768, 32767).astype("<i2")
        q.tofile(path)
    elif dtype == "s24le":
        q = np.clip(np.round(flat * float(1 << 23)), -(1 << 23), (1 << 23) - 1)
        q = q.astype(np.int32)
        u = q & 0xFFFFFF
        b = np.stack([u & 0xFF, (u >> 8) & 0xFF, (u >> 16) & 0xFF], axis=1)
        b.astype(np.uint8).tofile(path)
    elif dtype == "s32le":
        q = np.clip(np.round(flat * float(1 << 31)), -(1 << 31), (1 << 31) - 1)
        q.astype("<i4").tofile(path)
    elif dtype == "f32le":
        flat.astype("<f4").tofile(path)
    elif dtype == "f64le":
        flat.astype("<f8").tofile(path)
    else:
        raise ValueError(f"不支持的 PCM 格式: {dtype!r}")
