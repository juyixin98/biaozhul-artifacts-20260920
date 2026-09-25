"""验收指标：通带误差、混叠抑制等纯数值度量。"""

from __future__ import annotations

import numpy as np


def rms(x: np.ndarray) -> float:
    x = np.asarray(x, dtype=np.float64)
    if x.size == 0:
        return 0.0
    return float(np.sqrt(np.mean(x * x)))


def _steady(x: np.ndarray, skip: int) -> np.ndarray:
    """去掉首尾各 skip 个样本（边界暂态），取稳态区。"""
    if skip <= 0:
        return x
    if x.size <= 2 * skip:
        return x[:0]
    return x[skip:-skip]


def sine_fit(y: np.ndarray, freq: float, fs: float, skip: int = 0) -> dict:
    """对 y 做最小二乘正弦拟合（已知频率），返回幅度/相位/残差。

    拟合模型：y[n] = A*cos(w n) + B*sin(w n) + C，残差即通带误差。
    skip 为首尾各跳过的样本数（排除边界暂态）。
    """
    y = _steady(np.asarray(y, dtype=np.float64), skip)
    n = np.arange(skip, skip + y.size, dtype=np.float64)
    w = 2.0 * np.pi * freq / fs
    basis = np.column_stack([np.cos(w * n), np.sin(w * n), np.ones(y.size)])
    coef, *_ = np.linalg.lstsq(basis, y, rcond=None)
    residual = y - basis @ coef
    amplitude = float(np.hypot(coef[0], coef[1]))
    return {
        "amplitude": amplitude,
        "phase_rad": float(np.arctan2(coef[0], coef[1])),
        "dc_offset": float(coef[2]),
        "residual_rms": rms(residual),
        "residual_max": float(np.max(np.abs(residual))) if y.size else 0.0,
        "n_samples": int(y.size),
    }


def suppression_db(y: np.ndarray, reference_amplitude: float = 1.0, skip: int = 0) -> float:
    """稳态输出相对参考幅度的抑制量（dB，正值表示衰减）。

    skip 为首尾各跳过的样本数（排除边界暂态）。
    """
    y = _steady(np.asarray(y, dtype=np.float64), skip)
    level = rms(y)
    if level <= 0.0:
        return float("inf")
    return float(-20.0 * np.log10(level / (reference_amplitude / np.sqrt(2.0))))
