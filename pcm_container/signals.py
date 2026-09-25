"""离线合成信号（纯 NumPy，无录音/播放依赖）。

支持类型：
- sine：正弦波，可配频率、相位、振幅；
- square：占空比 50% 的方波（符号函数实现）；
- sawtooth：从 -1 线性上升到 +1 的锯齿波；
- chirp：线性扫频正弦（f0 -> f1）；
- noise：确定性伪随机白噪声，由整数 seed 驱动的 NumPy Generator，
  振幅在 [-amplitude, amplitude] 均匀分布（便于极值测试可复现）。

所有输出为 float64 一维数组，长度 = duration * sample_rate，不做重采样，
因此 duration*sample_rate 必须为整数帧。
"""

from __future__ import annotations

import numpy as np

from .errors import PcmAlignmentError, PcmError

WAVE_TYPES = ("sine", "square", "sawtooth", "chirp", "noise")


def synthesize(
    wave_type: str,
    sample_rate: int,
    duration: float,
    frequency: float = 440.0,
    amplitude: float = 0.8,
    phase: float = 0.0,
    frequency_end: float | None = None,
    seed: int = 0,
) -> np.ndarray:
    """合成一维 float64 信号。

    参数
    -----
    wave_type: sine / square / sawtooth / chirp / noise
    sample_rate: 采样率 Hz，必须为正整数
    duration: 时长秒，duration*sample_rate 必须为整数帧
    frequency: 基频（chirp 时为起始频率 f0；noise 忽略）
    amplitude: 线性振幅标量，结果统一乘以此值
    phase: 初相（弧度，仅 sine/square/sawtooth/chirp 有效）
    frequency_end: chirp 终止频率 f1；chirp 时必填
    seed: noise 的随机种子（确定性可复现）
    """
    if sample_rate < 1:
        raise PcmError(f"采样率必须为正整数，得到 {sample_rate}")
    if duration < 0:
        raise PcmError(f"时长不能为负，得到 {duration}")

    n_frames = duration * sample_rate
    if abs(n_frames - round(n_frames)) > 1e-9:
        raise PcmAlignmentError(
            f"duration({duration}) * sample_rate({sample_rate}) = {n_frames} "
            "不是整数帧，无法对齐采样"
        )
    n = int(round(n_frames))
    if n == 0:
        raise PcmError("信号长度为 0 帧")

    t = np.arange(n, dtype=np.float64) / np.float64(sample_rate)

    if wave_type == "sine":
        if frequency < 0:
            raise PcmError(f"频率不能为负，得到 {frequency}")
        out = np.sin(2.0 * np.pi * frequency * t + phase)
    elif wave_type == "square":
        if frequency < 0:
            raise PcmError(f"频率不能为负，得到 {frequency}")
        out = np.sign(np.sin(2.0 * np.pi * frequency * t + phase))
    elif wave_type == "sawtooth":
        if frequency <= 0:
            raise PcmError(f"锯齿波频率必须为正，得到 {frequency}")
        phase_frac = (phase / (2.0 * np.pi)) % 1.0
        cycles = frequency * t + phase_frac
        out = 2.0 * (cycles - np.floor(cycles)) - 1.0
    elif wave_type == "chirp":
        if frequency_end is None:
            raise PcmError("chirp 必须提供 frequency_end")
        if frequency < 0 or frequency_end < 0:
            raise PcmError("扫频端点不能为负")
        # 瞬时相位 = 2π(f0 t + (f1-f0) t^2 / (2T))
        t_end = np.float64(n) / np.float64(sample_rate)
        phase_inst = (
            2.0
            * np.pi
            * (
                frequency * t
                + (frequency_end - frequency) * t * t / (2.0 * t_end)
            )
            + phase
        )
        out = np.sin(phase_inst)
    elif wave_type == "noise":
        rng = np.random.default_rng(seed)
        out = rng.uniform(-1.0, 1.0, size=n)
    else:
        raise PcmError(
            f"未知信号类型 {wave_type!r}，可选：{', '.join(WAVE_TYPES)}"
        )

    return out * np.float64(amplitude)
