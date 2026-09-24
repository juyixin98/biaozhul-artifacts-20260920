"""本地 PCM 数据读写：原始交错 PCM 与整数 PCM WAV（仅后端文件处理）。

支持的原始 PCM 位深：``u8``（无符号 8 位）、``s16``、``s32``、``f32``。
立体声既可放在一个交错文件中，也可用两个单声道文件分别提供。
读入后统一归一化为 float64，幅度约在 ``[-1, 1)``；写回时按目标位深量化。
"""

from __future__ import annotations

import wave

import numpy as np

__all__ = [
    "SUPPORTED_DTYPES",
    "read_pcm",
    "write_pcm",
    "deinterleave",
    "read_wav",
    "write_wav",
]

SUPPORTED_DTYPES = ("u8", "s16", "s32", "f32")

_DTYPE_MAP = {
    "u8": np.uint8,
    "s16": np.int16,
    "s32": np.int32,
    "f32": np.float32,
}
# 整数格式的满幅刻度（f32 直接就是浮点幅度）。
_INT_SCALE = {"u8": 128.0, "s16": 32768.0, "s32": 2147483648.0}


def _decode(raw: np.ndarray, dtype_name: str) -> np.ndarray:
    """把原始整数样本解码为 float64，满幅映射到 [-1, 1)。"""
    if dtype_name == "f32":
        return raw.astype(np.float64)
    x = raw.astype(np.float64)
    if dtype_name == "u8":
        x -= 128.0  # u8 以 128 为零点
    return x / _INT_SCALE[dtype_name]


def _encode(x: np.ndarray, dtype_name: str) -> np.ndarray:
    """把 float64 样本量化回目标 PCM 格式（先夹幅，避免回绕）。"""
    if dtype_name == "f32":
        return x.astype(np.float32)
    scale = _INT_SCALE[dtype_name]
    q = np.clip(np.rint(x * scale), -scale, scale - 1.0)
    if dtype_name == "u8":
        q += 128.0
        return q.astype(np.uint8)
    return q.astype(_DTYPE_MAP[dtype_name])


def read_pcm(
    path: str,
    *,
    dtype: str = "s16",
    channels: int = 1,
    max_samples: int | None = None,
) -> np.ndarray:
    """读原始 PCM 文件。

    返回形状 ``(n_frames, channels)`` 的 float64 数组（单声道时形状为
    ``(n_frames, 1)``，调用方按需 ``[:, 0]``）。
    """
    if dtype not in _DTYPE_MAP:
        raise ValueError(f"不支持的 dtype {dtype!r}，可选：{SUPPORTED_DTYPES}")
    channels = int(channels)
    if channels < 1:
        raise ValueError("channels 必须 >= 1")
    raw = np.fromfile(path, dtype=_DTYPE_MAP[dtype])
    if raw.size % channels != 0:
        raise ValueError(
            f"文件 {path} 字节数不能被 {channels} 声道整除，数据可能损坏或参数错误"
        )
    decoded = _decode(raw, dtype).reshape(-1, channels)
    if max_samples is not None:
        decoded = decoded[: int(max_samples)]
    return decoded


def write_pcm(
    path: str,
    data: np.ndarray,
    *,
    dtype: str = "s16",
) -> None:
    """把 ``(n_frames, channels)`` 或一维 float 数据交错写入原始 PCM 文件。"""
    if dtype not in _DTYPE_MAP:
        raise ValueError(f"不支持的 dtype {dtype!r}，可选：{SUPPORTED_DTYPES}")
    arr = np.asarray(data, dtype=np.float64)
    if arr.ndim == 1:
        arr = arr[:, None]
    encoded = _encode(arr.ravel(), dtype)
    encoded.tofile(path)


def deinterleave(data: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """从 ``(n_frames, 2)`` 取出两个单声道。"""
    if data.ndim != 2 or data.shape[1] != 2:
        raise ValueError(f"期望 (n, 2) 的立体声数据，实际形状 {data.shape}")
    return data[:, 0].copy(), data[:, 1].copy()


def read_wav(path: str) -> tuple[np.ndarray, int]:
    """读整数 PCM WAV（8/16/24/32-bit）。

    返回 ``(data, sample_rate)``，``data`` 形状 ``(n_frames, channels)``，
    float64 幅度。24-bit 用手工字节拼装（wav 模块不直接支持）。
    """
    with wave.open(str(path), "rb") as wf:
        nch = wf.getnchannels()
        sw = wf.getsampwidth()
        fr = wf.getframerate()
        frames = wf.readframes(wf.getnframes())

    if sw == 1:
        raw = np.frombuffer(frames, dtype=np.uint8).astype(np.float64) - 128.0
        data = raw / 128.0
    elif sw == 2:
        data = np.frombuffer(frames, dtype="<i2").astype(np.float64) / 32768.0
    elif sw == 4:
        data = np.frombuffer(frames, dtype="<i4").astype(np.float64) / 2147483648.0
    elif sw == 3:
        b = np.frombuffer(frames, dtype=np.uint8).reshape(-1, 3)
        as32 = (
            b[:, 0].astype(np.int32)
            | (b[:, 1].astype(np.int32) << 8)
            | (b[:, 2].astype(np.int32) << 16)
        )
        as32 = np.where(as32 & 0x800000, as32 - 0x1000000, as32)
        data = as32.astype(np.float64) / 8388608.0
    else:
        raise ValueError(f"不支持的 WAV 位深：{sw * 8}-bit（仅支持整数 PCM）")
    return data.reshape(-1, nch).copy(), fr


def write_wav(path: str, data: np.ndarray, sample_rate: int, *, bits: int = 16) -> None:
    """写整数 PCM WAV（16/32-bit；另有 8-bit）。"""
    arr = np.asarray(data, dtype=np.float64)
    if arr.ndim == 1:
        arr = arr[:, None]
    nch = arr.shape[1]
    if bits == 16:
        pcm = np.clip(np.rint(arr.ravel() * 32768.0), -32768, 32767).astype("<i2")
        sw = 2
    elif bits == 32:
        pcm = np.clip(
            np.rint(arr.ravel() * 2147483648.0), -2147483648, 2147483647
        ).astype("<i4")
        sw = 4
    elif bits == 8:
        pcm = np.clip(
            np.rint(arr.ravel() * 128.0) + 128.0, 0, 255
        ).astype(np.uint8)
        sw = 1
    else:
        raise ValueError("WAV 仅支持 bits=8/16/32")
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(nch)
        wf.setsampwidth(sw)
        wf.setframerate(int(sample_rate))
        wf.writeframes(pcm.tobytes())
