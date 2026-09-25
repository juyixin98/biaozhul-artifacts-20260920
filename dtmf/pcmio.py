"""本地 PCM / WAV 文件读写（16bit 单声道）。"""

from __future__ import annotations

import wave
from pathlib import Path

import numpy as np


def read_pcm(path: str | Path, sample_rate: int | None = None) -> tuple[np.ndarray, int]:
    """读取音频文件，返回 (int16 单声道样本, 采样率)。

    - .wav：通过标准库 wave 读取，要求 16bit 单声道；
    - 其他后缀（如 .pcm/.raw）：按 s16le 原始 PCM 读取，此时必须给出 sample_rate。
    """
    path = Path(path)
    if path.suffix.lower() == ".wav":
        with wave.open(str(path), "rb") as wf:
            if wf.getnchannels() != 1:
                raise ValueError("仅支持单声道 WAV")
            if wf.getsampwidth() != 2:
                raise ValueError("仅支持 16bit WAV")
            data = wf.readframes(wf.getnframes())
            return np.frombuffer(data, dtype="<i2").copy(), wf.getframerate()
    if sample_rate is None:
        raise ValueError("读取原始 PCM 时必须指定 sample_rate")
    return np.fromfile(path, dtype="<i2"), sample_rate


def write_pcm(path: str | Path, samples: np.ndarray, sample_rate: int) -> None:
    """写出音频。.wav 后缀写标准 WAV，否则写 s16le 原始 PCM。"""
    path = Path(path)
    samples = np.asarray(samples, dtype=np.int16)
    if path.suffix.lower() == ".wav":
        with wave.open(str(path), "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(sample_rate)
            wf.writeframes(samples.astype("<i2").tobytes())
    else:
        samples.astype("<i2").tofile(path)
