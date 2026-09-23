"""本地 PCM / NumPy 数据读写。

支持格式：
  s16le  — 16 bit 有符号小端整型 PCM（映射到 [-1, 1) 浮点）
  f32le  — 32 bit 小端浮点 PCM
  f64le  — 64 bit 小端浮点 PCM
  npy    — NumPy .npy 文件
"""

from __future__ import annotations

import numpy as np

FORMATS = ("s16le", "f32le", "f64le", "npy")

_DTYPES = {
    "s16le": "<i2",
    "f32le": "<f4",
    "f64le": "<f8",
}


def read_pcm(path: str, fmt: str) -> np.ndarray:
    """读取本地 PCM 数据，返回 float64 数组。"""
    if fmt == "npy":
        return np.load(path).astype(np.float64)
    if fmt not in _DTYPES:
        raise ValueError(f"不支持的格式 {fmt!r}，可选 {FORMATS}")
    raw = np.fromfile(path, dtype=np.dtype(_DTYPES[fmt]))
    if fmt == "s16le":
        return raw.astype(np.float64) / 32768.0
    return raw.astype(np.float64)


def write_pcm(path: str, x: np.ndarray, fmt: str) -> None:
    """把 float 数组写入本地文件。s16le 会先裁剪到 [-1, 1]。"""
    x = np.asarray(x, dtype=np.float64)
    if fmt == "npy":
        np.save(path, x)
        return
    if fmt not in _DTYPES:
        raise ValueError(f"不支持的格式 {fmt!r}，可选 {FORMATS}")
    if fmt == "s16le":
        data = np.clip(x, -1.0, 1.0 - 1.0 / 32768.0)
        out = np.round(data * 32768.0).astype("<i2")
    else:
        out = x.astype(np.dtype(_DTYPES[fmt]))
    out.tofile(path)
