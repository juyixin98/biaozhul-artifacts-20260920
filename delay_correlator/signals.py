"""信号来源：合成测试信号生成，以及本地原始 PCM / WAV 读取。

合成信号是离线验证相关器的主要手段：给定已知延迟构造两通道，再把估计结果与真值比较。
文件读取支持原始裸 PCM（1/2/4 字节整型或 32 位浮点）与整数 PCM WAV（8/16/32 位）。
读出的采样一律换算为 float64，满量程归一化到 1.0。
"""

from __future__ import annotations

import wave
from dataclasses import dataclass

import numpy as np

SIGNAL_KINDS = ("noise", "sine", "chirp", "mixed")

# 裸 PCM dtype 名称 -> numpy 类型（无后缀默认小端）。
_PCM_DTYPES = {
    "s16": "<i2",
    "s16le": "<i2",
    "s16be": ">i2",
    "s32": "<i4",
    "s32le": "<i4",
    "s32be": ">i4",
    "u8": "u1",
    "f32": "<f4",
    "f32le": "<f4",
    "f32be": ">f4",
    "float32": "<f4",
}

# 整型 PCM 满量程绝对值，用于换算到 [-1, 1]。
_INT_PEAK = {"u1": 128.0, "i2": 32768.0, "i4": 2147483648.0}


@dataclass(frozen=True)
class SyntheticSpec:
    """合成信号规格（对应请求 JSON 的 ``source`` 段，``mode="synthetic"``）。"""

    kind: str = "noise"
    duration: float = 1.0
    sample_rate: int = 8000
    delay_samples: int = 0
    noise_db: float | None = None
    gain: float = 1.0
    amplitude: float = 0.5
    seed: int = 0
    freq: float = 440.0
    f0: float = 200.0
    f1: float = 1000.0
    freqs: tuple[float, ...] = (440.0, 880.0, 1320.0)

    @classmethod
    def from_dict(cls, data: dict) -> "SyntheticSpec":
        # "mode" 属于请求 envelope 字段（synthetic/file 选择器）；
        # 下划线开头的键（如 "_comment"）视为注释，均不参与解析。
        fields = {
            k: v
            for k, v in data.items()
            if k != "mode" and not k.startswith("_")
        }
        known = set(cls.__dataclass_fields__)  # type: ignore[attr-defined]
        unknown = set(fields) - known
        if unknown:
            raise ValueError(f"source(synthetic) 含未知参数: {sorted(unknown)}")
        payload = dict(fields)
        if "freqs" in payload and payload["freqs"] is not None:
            payload["freqs"] = tuple(float(x) for x in payload["freqs"])
        spec = cls(**payload)
        if spec.kind not in SIGNAL_KINDS:
            raise ValueError(f"kind 必须是 {SIGNAL_KINDS} 之一，收到 {spec.kind!r}")
        if spec.duration <= 0:
            raise ValueError("duration 必须为正数（秒）")
        if spec.sample_rate <= 0:
            raise ValueError("sample_rate 必须为正整数")
        if abs(spec.delay_samples) >= int(spec.duration * spec.sample_rate) // 2:
            raise ValueError("delay_samples 的绝对值过大：信号需为延迟两侧留有内容")
        return spec


def generate_source(spec: SyntheticSpec) -> np.ndarray:
    """按规格生成单路基准源信号（float64）。"""
    n = int(round(spec.duration * spec.sample_rate))
    t = np.arange(n, dtype=np.float64) / spec.sample_rate
    a = spec.amplitude

    if spec.kind == "noise":
        rng = np.random.default_rng(spec.seed)
        x = rng.standard_normal(n)
        x *= a / (float(np.sqrt(np.mean(x**2))) or 1.0)  # RMS 归一化到 amplitude
    elif spec.kind == "sine":
        x = a * np.sin(2.0 * np.pi * spec.freq * t)
    elif spec.kind == "chirp":
        # 线性扫频：瞬时频率 f0 -> f1，天然非周期，相关峰唯一。
        span = max(spec.f1 - spec.f0, 1.0)
        phase = 2.0 * np.pi * (spec.f0 * t + 0.5 * span * t**2 / max(spec.duration, 1e-9))
        x = a * np.sin(phase)
    else:  # mixed：多个正弦之和
        x = np.zeros(n, dtype=np.float64)
        for f in spec.freqs:
            x += np.sin(2.0 * np.pi * f * t)
        peak = float(np.max(np.abs(x))) or 1.0
        x *= a / peak
    return x


def shift_signal(x: np.ndarray, delay_samples: int) -> np.ndarray:
    """右移 ``delay_samples`` 个采样点（左端补零）；负值表示左移（右端补零）。"""
    d = int(delay_samples)
    out = np.zeros_like(x)
    if d > 0:
        out[d:] = x[:-d]
    elif d < 0:
        out[:d] = x[-d:]  # d 为负：out[0:len+d] = x[-d:len]
    else:
        out[:] = x
    return out


def _add_noise(x: np.ndarray, noise_db: float | None, rng: np.random.Generator) -> np.ndarray:
    """按相对信号 RMS 的 dB 电平叠加高斯白噪声；noise_db=None 表示不加噪。"""
    if noise_db is None:
        return x.copy()
    rms = float(np.sqrt(np.mean(x**2)))
    noise_rms = (rms if rms > 0 else 1.0) * (10.0 ** (noise_db / 20.0))
    return x + rng.standard_normal(x.size) * noise_rms


def make_delayed_pair(
    spec: SyntheticSpec,
) -> tuple[np.ndarray, np.ndarray]:
    """构造 (ref, chan)：``chan = gain * shift(ref_ideal, delay)``，两通道各加独立噪声。

    符号约定：``delay_samples > 0`` 时 chan 相对 ref 右移（chan 滞后）。
    """
    base = generate_source(spec)
    rng = np.random.default_rng(spec.seed + 1)
    ref = _add_noise(base, spec.noise_db, rng)
    chan = _add_noise(spec.gain * shift_signal(base, spec.delay_samples), spec.noise_db, rng)
    return ref.astype(np.float64), chan.astype(np.float64)


def _decode_int_pcm(raw: bytes, sampwidth: int) -> np.ndarray:
    """解码小端整数 PCM（WAV 标准为小端），8 位无符号、16/32 位有符号。"""
    if sampwidth == 1:
        return (np.frombuffer(raw, dtype=np.uint8).astype(np.float64) - 128.0) / 128.0
    if sampwidth == 2:
        return np.frombuffer(raw, dtype="<i2").astype(np.float64) / 32768.0
    if sampwidth == 4:
        return np.frombuffer(raw, dtype="<i4").astype(np.float64) / 2147483648.0
    raise ValueError(f"仅支持 8/16/32 位整数 PCM WAV，收到 sampwidth={sampwidth}")


def read_wav(
    path: str,
    channel_index: int = 0,
    max_samples: int | None = None,
) -> tuple[np.ndarray, int]:
    """读取 WAV 的指定声道，返回 ``(float64 信号, 采样率)``。

    只支持无压缩整数 PCM（WAVE_FORMAT_PCM）。立体声取 ``channel_index``
    （0=左，1=右）；单声道文件会忽略该参数。
    """
    with wave.open(path, "rb") as wf:
        nch = wf.getnchannels()
        sampwidth = wf.getsampwidth()
        sr = wf.getframerate()
        frames = wf.readframes(wf.getnframes())
    if nch < 1:
        raise ValueError(f"WAV 声道数异常: {nch}")
    if channel_index < 0 or channel_index >= nch:
        raise ValueError(f"channel_index={channel_index} 超出 WAV 声道范围 [0, {nch - 1}]")
    x = _decode_int_pcm(frames, sampwidth)
    if nch > 1:
        x = x.reshape(-1, nch)[:, channel_index]
    if max_samples is not None:
        x = x[: int(max_samples)]
    return np.ascontiguousarray(x, dtype=np.float64), int(sr)


def read_raw_pcm(
    path: str,
    dtype: str = "s16",
    channels: int = 1,
    channel_index: int = 0,
    sample_rate: int | None = None,
    max_samples: int | None = None,
) -> tuple[np.ndarray, int | None]:
    """读取裸 PCM 文件的指定声道，返回 ``(float64 信号, sample_rate)``。

    ``dtype`` 见 :data:`_PCM_DTYPES`（如 ``s16``/``s32``/``u8``/``f32``，
    可加 ``le``/``be`` 后缀指定端序）。多声道数据按帧交错存储。
    裸文件不含采样率头，``sample_rate`` 仅原样回传以便上游记录。
    """
    key = dtype.lower()
    if key not in _PCM_DTYPES:
        raise ValueError(f"不支持的 PCM dtype {dtype!r}，可选: {sorted(set(_PCM_DTYPES))}")
    if channels < 1:
        raise ValueError("channels 必须 >=1")
    if channel_index < 0 or channel_index >= channels:
        raise ValueError(f"channel_index={channel_index} 超出 channels={channels}")

    np_dtype = np.dtype(_PCM_DTYPES[key])
    raw = np.fromfile(path, dtype=np_dtype)
    if channels > 1:
        usable = (raw.size // channels) * channels
        raw = raw[:usable].reshape(-1, channels)[:, channel_index]
    x = raw.astype(np.float64)
    kind = np_dtype.kind
    if kind == "u":
        x = (x - 128.0) / 128.0
    elif kind == "i":
        itemsize = np_dtype.itemsize
        x /= float(1 << (8 * itemsize - 1))
    # 浮点 PCM 视为已归一化，不缩放。
    if max_samples is not None:
        x = x[: int(max_samples)]
    return np.ascontiguousarray(x, dtype=np.float64), sample_rate


def save_raw_pcm(
    path: str,
    ref: np.ndarray,
    chan: np.ndarray,
    dtype: str = "s16",
    interleaved: bool = False,
) -> None:
    """把两路信号写为裸 PCM（样例与测试用）。

    interleaved=True 时单文件内帧交错双声道；否则两个文件顺序写入同一文件
    （测试自读用，请求接口不产生这种格式）。
    """
    key = dtype.lower()
    if key not in _PCM_DTYPES:
        raise ValueError(f"不支持的 PCM dtype {dtype!r}")
    np_dtype = np.dtype(_PCM_DTYPES[key])

    def encode(x: np.ndarray) -> np.ndarray:
        x = np.clip(x, -1.0, 1.0)
        kind = np_dtype.kind
        if kind == "u":
            # u8 范围 0..255，零点 128；用 127 防 +1 越界。
            return np.round(x * 127.0 + 128.0).clip(0, 255).astype(np_dtype)
        if kind == "i":
            # 有符号整型正负不对称（如 s16: -32768..32767），上界用 peak-1 防溢出环绕。
            peak = float(1 << (8 * np_dtype.itemsize - 1))
            return np.round(x * (peak - 1)).astype(np_dtype)
        return x.astype(np_dtype)

    a, b = encode(ref), encode(chan)
    if interleaved:
        # 帧交错：偶数位为第 0 声道（ref），奇数位为第 1 声道（chan）。
        interleaved_out = np.empty(2 * a.size, dtype=np_dtype)
        interleaved_out[0::2] = a
        interleaved_out[1::2] = b
        interleaved_out.tofile(path)
    else:
        # 顺序拼接（测试自读用；生产请求不产生这种格式）。
        np.concatenate((a, b)).tofile(path)


def write_wav16(path: str, signals: list[np.ndarray], sample_rate: int) -> None:
    """写 16 位 PCM WAV（单声道一路，或多路交错），供样例与测试使用。"""
    n = min(len(s) for s in signals)
    nch = len(signals)
    stacked = np.empty(n * nch, dtype="<i2")
    for ch, s in enumerate(signals):
        clipped = np.clip(np.asarray(s[:n], dtype=np.float64), -1.0, 1.0)
        stacked[ch :: nch] = np.round(clipped * 32767.0).astype("<i2")
    with wave.open(path, "wb") as wf:
        wf.setnchannels(nch)
        wf.setsampwidth(2)
        wf.setframerate(int(sample_rate))
        wf.writeframes(stacked.tobytes())
