"""可配置项。

规模上限、对称性相对容差、残差/正交性阈值、迭代预算倍率等均可通过
环境变量或直接构造 :class:`EigenConfig` 覆盖。
"""

from __future__ import annotations

import os
from dataclasses import dataclass

ENV_PREFIX = "EIGEN_"


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(ENV_PREFIX + name)
    return float(raw) if raw is not None else default


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(ENV_PREFIX + name)
    return int(raw) if raw is not None else default


@dataclass(frozen=True)
class EigenConfig:
    """一次特征分解所使用的全部数值/预算配置。"""

    # --- 规模 ---
    max_size: int = 512
    min_size: int = 1

    # --- 输入对称性 (相对容差): ||A - A^T||_F <= sym_tol * ||A||_F ---
    sym_tol: float = 1e-9

    # --- 后验证据阈值 (相对, 除以 ||A||_F) ---
    residual_tol: float = 1e-8
    orthogonality_tol: float = 1e-8
    reconstruction_tol: float = 1e-8
    # 特征值重分组间隙: 间隙 <= gap_tol * scale 视为 (近) 重特征值
    gap_tol: float = 1e-7

    # --- 迭代预算: max_sweeps = max(base_sweeps, sweep_multiplier * n) ---
    base_sweeps: int = 30
    sweep_multiplier: int = 12

    # --- QR 内层 deflation 停止判据 (相对, 除以矩阵整体尺度) ---
    eig_tol: float = 1e-14

    # --- mpmath 高精度参考路径 ---
    mp_precision: int = 80  # 十进制位 (digits)

    @classmethod
    def from_env(cls) -> "EigenConfig":
        """从 ``EIGEN_*`` 环境变量构造 (未设置的项取默认值)。"""
        values = {
            "max_size": _env_int("MAX_SIZE", 512),
            "min_size": _env_int("MIN_SIZE", 1),
            "sym_tol": _env_float("SYM_TOL", 1e-9),
            "residual_tol": _env_float("RESIDUAL_TOL", 1e-8),
            "orthogonality_tol": _env_float("ORTHOGONALITY_TOL", 1e-8),
            "reconstruction_tol": _env_float("RECONSTRUCTION_TOL", 1e-8),
            "gap_tol": _env_float("GAP_TOL", 1e-7),
            "base_sweeps": _env_int("BASE_SWEEPS", 30),
            "sweep_multiplier": _env_int("SWEEP_MULTIPLIER", 12),
            "eig_tol": _env_float("EIG_TOL", 1e-14),
            "mp_precision": _env_int("MP_PRECISION", 80),
        }
        return cls(**values)

    def max_sweeps_for(self, n: int) -> int:
        """规模相关的迭代预算。"""
        return max(self.base_sweeps, self.sweep_multiplier * n)
