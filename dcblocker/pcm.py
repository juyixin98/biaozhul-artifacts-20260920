"""本地 PCM 原始数据读写（无依赖，仅 NumPy）。

支持格式：int16 / int32 / float32 / float64，单声道。
整型按满量程归一化到 [-1, 1) 浮点；写出时反向量化并饱和截断。
"""

from __future__ import annotations

import numpy as np

_INT_FORMATS = {
    "int16": (np.dtype("<i2"), 32768.0),
    "int32": (np.dtype("<i4"), 2147483648.0),
}
_FLOAT_FORMATS = {
    "float32": np.dtype("<f4"),
    "float64": np.dtype("<f8"),
}

SUPPORTED_FORMATS = sorted([*_INT_FORMATS, *_FLOAT_FORMATS])


def read_pcm(path: str, fmt: str) -> np.ndarray:
    """读取 PCM 文件，返回 float64 一维数组（整型已归一化）。"""
    if fmt in _INT_FORMATS:
        dtype, full_scale = _INT_FORMATS[fmt]
        raw = np.fromfile(path, dtype=dtype)
        return raw.astype(np.float64) / full_scale
    if fmt in _FLOAT_FORMATS:
        return np.fromfile(path, dtype=_FLOAT_FORMATS[fmt]).astype(np.float64)
    raise ValueError(f"不支持的 PCM 格式 {fmt!r}，支持：{SUPPORTED_FORMATS}")


def write_pcm(path: str, data: np.ndarray, fmt: str) -> None:
    """把 float 数组写入 PCM 文件。整型格式做饱和量化。"""
    x = np.asarray(data, dtype=np.float64)
    if fmt in _INT_FORMATS:
        dtype, full_scale = _INT_FORMATS[fmt]
        lo, hi = np.iinfo(dtype).min, np.iinfo(dtype).max
        quantized = np.clip(np.round(x * full_scale), lo, hi).astype(dtype)
        quantized.tofile(path)
        return
    if fmt in _FLOAT_FORMATS:
        x.astype(_FLOAT_FORMATS[fmt]).tofile(path)
        return
    raise ValueError(f"不支持的 PCM 格式 {fmt!r}，支持：{SUPPORTED_FORMATS}")


def format_for_extension(path: str) -> str:
    """按扩展名推断 PCM 格式：.i16/.i32/.f32/.f64。"""
    table = {".i16": "int16", ".i32": "int32", ".f32": "float32", ".f64": "float64"}
    for ext, fmt in table.items():
        if path.endswith(ext):
            return fmt
    raise ValueError(f"无法从扩展名推断 PCM 格式：{path}（支持 {sorted(table)}）")
