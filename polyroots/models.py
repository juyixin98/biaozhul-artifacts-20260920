"""模块间数据契约：纯 dataclass，不依赖 numpy 以外的东西。

约定系数顺序：descending（a_0 x^n + ... + a_n）或 ascending（a_0 + a_1 x + ...）。
内部统一使用 ascending。
"""

from __future__ import annotations

import dataclasses
import enum
from typing import Any

import numpy as np


class CoeffOrder(str, enum.Enum):
    DESCENDING = "descending"  # 最高次在前，最常见（numpy.roots 约定）
    ASCENDING = "ascending"    # 常数项在前（np.polynomial 约定）


class KernelName(str, enum.Enum):
    COMPANION = "companion"  # 伴随矩阵特征值（LAPACK QR/ZGEEV）
    ABERTH = "aberth"        # Aberth–Erhlich 同时迭代（带单根收敛状态）
    AUTO = "auto"            # n <= 100 用 companion，否则 aberth


class SolveStatus(str, enum.Enum):
    CONVERGED = "converged"
    NOT_CONVERGED = "not_converged"  # 迭代耗尽：保留已收敛根 + 未收敛根，不抛异常


class RootKind(str, enum.Enum):
    SIMPLE = "simple"
    NEAR_REPEATED = "near_repeated"  # 位于近重根簇内
    ZERO = "zero"                    # 精确/数值零根
    UNCONVERGED = "unconverged"      # 该根未达收敛阈值


@dataclasses.dataclass(frozen=True)
class SolveOptions:
    kernel: KernelName = KernelName.AUTO
    max_iterations: int = 200
    convergence_tol: float = 1e-12
    cluster_tol: float = 1e-6
    conjugate_tol: float = 1e-8
    max_degree: int = 256
    seed: int = 20260927

    def validate(self) -> None:
        if self.max_iterations <= 0:
            from .errors import InvalidOptionError
            raise InvalidOptionError(
                "max_iterations 必须为正整数", {"max_iterations": self.max_iterations}
            )
        for name in ("convergence_tol", "cluster_tol", "conjugate_tol"):
            val = getattr(self, name)
            if not (0.0 < val < 1.0):
                from .errors import InvalidOptionError
                raise InvalidOptionError(
                    f"{name} 必须落在半开区间 (0, 1)", {name: val}
                )
        if self.cluster_tol < self.convergence_tol:
            from .errors import InvalidOptionError
            raise InvalidOptionError(
                "cluster_tol 不应小于 convergence_tol（会导致近重根永远不可判）",
                {"cluster_tol": self.cluster_tol,
                 "convergence_tol": self.convergence_tol},
            )
        if self.max_degree < 1:
            from .errors import InvalidOptionError
            raise InvalidOptionError("max_degree 至少为 1", {"max_degree": self.max_degree})


@dataclasses.dataclass(frozen=True)
class RootEvidence:
    """单根误差证据。

    relative_residual: |p(z)| / (|a_n| * prod(1+|z_i|) 型尺度)
        —— 用首一化多项式在根尺度上的相对残差，避免“大系数小残差”的假象。
    cluster_id: 所属近重根簇编号（无则 -1），簇大小见 cluster_size。
    cluster_separation: 与簇内最近邻的相对间距；近重根时该值很小，
        即使 residual 小也不据此声称绝对准确。
    sensitivity_indicator: 条件数指示 ≈ 1 / prod_{j!=i}|z_i-z_j|（带尺度）。
        值越大，该根对系数扰动越敏感（近重根必然巨大）。
    converged: 内核视角该根是否达到 tol（companion 内核无法逐根判定，置 True
        并在 note 中说明，整体可信度看 factor_error / condition 证据）。
    iterations: Aberth 该根实际迭代数（companion 为 None）。
    note: 人类可读判断理由。
    """

    index: int
    relative_residual: float
    cluster_id: int
    cluster_size: int
    cluster_separation: float
    sensitivity_indicator: float
    converged: bool
    iterations: int | None
    note: str


@dataclasses.dataclass(frozen=True)
class RootRecord:
    value: complex
    kind: RootKind
    conjugate_of: int | None  # 稳定排序后配对根的下标；无则 None
    evidence: RootEvidence

    def to_dict(self) -> dict[str, Any]:
        return {
            "index": self.evidence.index,
            "real": self.value.real,
            "imag": self.value.imag,
            "kind": self.kind.value,
            "conjugate_of": self.conjugate_of,
            "converged": self.evidence.converged,
            "iterations": self.evidence.iterations,
            "relative_residual": self.evidence.relative_residual,
            "cluster_id": self.evidence.cluster_id,
            "cluster_size": self.evidence.cluster_size,
            "cluster_separation": self.evidence.cluster_separation,
            "sensitivity_indicator": self.evidence.sensitivity_indicator,
            "note": self.evidence.note,
        }


@dataclasses.dataclass(frozen=True)
class FactorError:
    """整体因子重构误差：由求得的根重构系数，与输入比较。

    float64 逐次相乘在稀疏多项式（大量零系数）上会发生灾难性消去：
    例如 x^64-1 的根精确到 1e-14，但 float64 重构对零系数的严格误差可达 1e-1。
    因此同时给出：
    - max_rel_coeff_error:   包络归一化（除以 Σ 积模长），消去稳健，为主判据；
    - strict_float64_error:  严格除以 (1+|c_k|)，会暴露原始消去，仅作诊断；
    - high_precision_error:  mpmath 高精度重构同一批 float64 根，独立算术复核，
                             为 None 表示未执行；
    - cancellation_ratio: strict/envelope，很大时说明严格误差由消去主导。
    """

    max_rel_coeff_error: float
    rms_rel_coeff_error: float
    constant_term_error: float
    strict_float64_error: float
    high_precision_error: float | None
    cancellation_ratio: float
    zero_coeff_count: int
    reconstructed_coeffs_asc: list[complex]


@dataclasses.dataclass(frozen=True)
class VietaCheck:
    """Vieta 关系抽查：和（= -a_{n-1}/a_n）、积（= (-1)^n a_0/a_n）。"""

    sum_abs_error: float
    sum_rel_error: float
    product_abs_error: float
    product_rel_error: float


@dataclasses.dataclass(frozen=True)
class KernelReport:
    name: str
    iterations_used: int
    converged_count: int
    unconverged_count: int
    intermediate: dict[str, Any]  # 关键中间状态（末轮校正量等），写入运行日志


@dataclasses.dataclass(frozen=True)
class SolveResult:
    run_id: str
    status: SolveStatus
    degree: int
    normalized_coeffs_asc: np.ndarray  # numpy，内部用
    roots: list[RootRecord]
    factor_error: FactorError
    vieta: VietaCheck
    kernel: KernelReport
    options: SolveOptions
    warnings: list[str]

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "status": self.status.value,
            "degree": self.degree,
            "roots": [r.to_dict() for r in self.roots],
            "factor_error": {
                "max_rel_coeff_error": self.factor_error.max_rel_coeff_error,
                "rms_rel_coeff_error": self.factor_error.rms_rel_coeff_error,
                "constant_term_error": self.factor_error.constant_term_error,
                "strict_float64_error": self.factor_error.strict_float64_error,
                "high_precision_error": self.factor_error.high_precision_error,
                "cancellation_ratio": self.factor_error.cancellation_ratio,
                "zero_coeff_count": self.factor_error.zero_coeff_count,
            },
            "vieta": dataclasses.asdict(self.vieta),
            "kernel": {
                "name": self.kernel.name,
                "iterations_used": self.kernel.iterations_used,
                "converged_count": self.kernel.converged_count,
                "unconverged_count": self.kernel.unconverged_count,
            },
            "warnings": self.warnings,
            "tolerances": {
                "convergence_tol": self.options.convergence_tol,
                "cluster_tol": self.options.cluster_tol,
                "conjugate_tol": self.options.conjugate_tol,
            },
        }
