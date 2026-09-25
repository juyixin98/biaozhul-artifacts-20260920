"""本地 PCM 原始数据读取（无文件头，需指定数据类型与采样率）。

整型 PCM 归一化到 [-1, 1)；浮点 PCM 原样读取。仅支持单声道；
多声道文件请先离线混音/抽取，避免误把交错声道当单声道分析。
"""

from __future__ import annotations

import numpy as np

#: dtype 名称 -> (numpy dtype, 是否为整型)
PCM_DTYPES: dict[str, tuple[np.dtype, bool]] = {
    "int16": (np.dtype("<i2"), True),
    "int32": (np.dtype("<i4"), True),
    "uint8": (np.dtype("<u1"), True),
    "float32": (np.dtype("<f4"), False),
    "float64": (np.dtype("<f8"), False),
}


def load_pcm(
    path: str,
    dtype: str = "int16",
    max_samples: int | None = None,
    offset_samples: int = 0,
) -> np.ndarray:
    """读取 PCM 文件，返回 float64 数组（整型已归一化到 [-1, 1)）。"""
    if dtype not in PCM_DTYPES:
        raise ValueError(f"不支持的 PCM 类型: {dtype!r}，可选 {sorted(PCM_DTYPES)}")
    np_dtype, is_int = PCM_DTYPES[dtype]
    if offset_samples < 0:
        raise ValueError(f"offset_samples 不能为负: {offset_samples}")

    data = np.fromfile(path, dtype=np_dtype)
    if offset_samples:
        if offset_samples >= data.size:
            raise ValueError(
                f"offset_samples={offset_samples} 超出文件样本数 {data.size}"
            )
        data = data[offset_samples:]
    if max_samples is not None:
        if max_samples <= 0:
            raise ValueError(f"max_samples 必须为正: {max_samples}")
        data = data[:max_samples]
    if data.size == 0:
        raise ValueError(f"文件 {path!r} 中没有可用样本")

    if is_int:
        info = np.iinfo(np_dtype)
        scale = float(max(abs(info.min), info.max))
        return data.astype(np.float64) / scale
    return data.astype(np.float64)


def save_pcm_int16(path: str, samples: np.ndarray) -> None:
    """把 [-1, 1] 浮点信号量化为 int16 PCM 写出（用于生成测试/样例数据）。"""
    x = np.asarray(samples, dtype=np.float64)
    # 与 load_pcm 的归一化一致：满幅 32768，正峰截到 32767
    np.clip(np.round(x * 32768.0), -32768, 32767).astype("<i2").tofile(path)
