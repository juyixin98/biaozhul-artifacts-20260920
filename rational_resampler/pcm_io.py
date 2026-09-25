"""本地 PCM / WAV 文件读写。

仅做文件与数值的转换，不涉及任何播放或界面：
- 原始 PCM：s16le / s32le / f32le，单声道，读入归一化到 float64 [-1, 1]；
- WAV：标准 16-bit PCM 单声道（stdlib wave 模块）。
"""

from __future__ import annotations

import wave

import numpy as np

_INT_DTYPES = {
    "s16le": (np.dtype("<i2"), 32768.0),
    "s32le": (np.dtype("<i4"), 2147483648.0),
}
_FLOAT_DTYPES = {
    "f32le": np.dtype("<f4"),
}
PCM_FORMATS = tuple(_INT_DTYPES) + tuple(_FLOAT_DTYPES) + ("wav",)


def read_pcm(path: str, fmt: str = "s16le") -> np.ndarray:
    """读取原始 PCM 文件，返回 float64 归一化数组。"""
    if fmt in _INT_DTYPES:
        dtype, scale = _INT_DTYPES[fmt]
        data = np.fromfile(path, dtype=dtype)
        return data.astype(np.float64) / scale
    if fmt in _FLOAT_DTYPES:
        return np.fromfile(path, dtype=_FLOAT_DTYPES[fmt]).astype(np.float64)
    raise ValueError(f"unsupported pcm format: {fmt!r}; use one of {PCM_FORMATS}")


def write_pcm(path: str, x: np.ndarray, fmt: str = "s16le") -> None:
    """把 float64 数组写入原始 PCM 文件（饱和截断到 [-1, 1)）。"""
    x = np.asarray(x, dtype=np.float64)
    if fmt in _INT_DTYPES:
        dtype, scale = _INT_DTYPES[fmt]
        clipped = np.clip(x, -1.0, 1.0 - 1.0 / scale)
        np.rint(clipped * scale).astype(dtype).tofile(path)
        return
    if fmt in _FLOAT_DTYPES:
        x.astype(_FLOAT_DTYPES[fmt]).tofile(path)
        return
    raise ValueError(f"unsupported pcm format: {fmt!r}; use one of {PCM_FORMATS}")


def read_wav(path: str) -> tuple[np.ndarray, int]:
    """读取 16-bit PCM 单声道 WAV，返回 (float64 数组, 采样率)。"""
    with wave.open(path, "rb") as w:
        if w.getsampwidth() != 2 or w.getnchannels() != 1:
            raise ValueError("only 16-bit mono PCM WAV is supported")
        fs = w.getframerate()
        raw = w.readframes(w.getnframes())
    return np.frombuffer(raw, dtype="<i2").astype(np.float64) / 32768.0, fs


def write_wav(path: str, x: np.ndarray, fs: int) -> None:
    """写入 16-bit PCM 单声道 WAV。"""
    x = np.asarray(x, dtype=np.float64)
    clipped = np.clip(x, -1.0, 1.0 - 1.0 / 32768.0)
    data = np.rint(clipped * 32768.0).astype("<i2")
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(int(round(fs)))
        w.writeframes(data.tobytes())


def read_any(path: str, fmt: str) -> tuple[np.ndarray, int | None]:
    """统一读取入口。WAV 返回采样率，原始 PCM 返回 None（由请求提供 fs）。"""
    if fmt == "wav":
        x, fs = read_wav(path)
        return x, fs
    return read_pcm(path, fmt), None


def write_any(path: str, x: np.ndarray, fmt: str, fs: float) -> None:
    """统一写出入口。"""
    if fmt == "wav":
        write_wav(path, x, int(round(fs)))
    else:
        write_pcm(path, x, fmt)
