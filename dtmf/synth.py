"""合成双音信号生成器（仅用于离线测试与验收）。

支持参数化：按键序列、音长/间隔、幅度、twist（高低音幅度比）、
频率偏移（百分比）、加性高斯白噪声（按目标 SNR 注入）。
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from .tones import KEY_TO_FREQS


@dataclass
class SynthConfig:
    sample_rate: int = 8000
    tone_ms: float = 60.0          # 每个音的持续时间
    gap_ms: float = 40.0           # 音间静音间隔（0 表示相邻音直接切换）
    amplitude: float = 0.5         # 单音幅度（满幅 1.0 对应 int16 满量程）
    twist_db: float = 0.0          # 行频相对列频的电平差（dB），即幅度比
    freq_deviation_pct: float = 0.0  # 频率偏移百分比，作用于全部 8 个频点
    snr_db: float | None = None    # 目标信噪比；None 表示不加噪声
    lead_silence_ms: float = 20.0  # 序列前导静音
    tail_silence_ms: float = 20.0  # 序列结尾静音
    seed: int | None = None        # 噪声随机种子，便于复现


def _tone(freqs: tuple[float, float], n: int, fs: int, amplitude: float,
          twist_db: float, deviation_pct: float) -> np.ndarray:
    t = np.arange(n, dtype=np.float64) / fs
    scale = 1.0 + deviation_pct / 100.0
    row_amp = amplitude * 10.0 ** (twist_db / 20.0)
    col_amp = amplitude
    # 限制峰值不超过 1.0，避免后续 int16 转换削波
    peak = row_amp + col_amp
    if peak > 1.0:
        row_amp /= peak
        col_amp /= peak
    f_row, f_col = freqs[0] * scale, freqs[1] * scale
    return row_amp * np.sin(2 * np.pi * f_row * t) + col_amp * np.sin(2 * np.pi * f_col * t)


def synthesize(keys: str, config: SynthConfig | None = None) -> tuple[np.ndarray, int]:
    """合成按键序列，返回 (float64 信号, 采样率)。

    未知按键字符会抛出 ValueError。
    """
    cfg = config or SynthConfig()
    fs = cfg.sample_rate
    for k in keys:
        if k not in KEY_TO_FREQS:
            raise ValueError(f"未知按键: {k!r}（合法按键: {''.join(sorted(KEY_TO_FREQS))}）")

    n_tone = int(round(cfg.tone_ms * fs / 1000.0))
    n_gap = int(round(cfg.gap_ms * fs / 1000.0))
    parts: list[np.ndarray] = [np.zeros(int(round(cfg.lead_silence_ms * fs / 1000.0)))]
    for i, k in enumerate(keys):
        parts.append(_tone(KEY_TO_FREQS[k], n_tone, fs, cfg.amplitude,
                           cfg.twist_db, cfg.freq_deviation_pct))
        if i < len(keys) - 1 and n_gap > 0:
            parts.append(np.zeros(n_gap))
    parts.append(np.zeros(int(round(cfg.tail_silence_ms * fs / 1000.0))))
    signal = np.concatenate(parts) if parts else np.zeros(0)

    if cfg.snr_db is not None:
        rng = np.random.default_rng(cfg.seed)
        tone_power = float(np.mean(signal ** 2))
        if tone_power > 0.0:
            noise_power = tone_power / (10.0 ** (cfg.snr_db / 10.0))
            signal = signal + rng.normal(0.0, np.sqrt(noise_power), size=signal.shape)

    return signal, fs


def to_int16(signal: np.ndarray) -> np.ndarray:
    """float 信号（[-1, 1] 附近）转 int16 PCM，超出范围截断。"""
    clipped = np.clip(signal, -1.0, 1.0)
    return np.round(clipped * 32767.0).astype(np.int16)
