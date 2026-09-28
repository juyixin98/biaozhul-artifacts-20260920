"""服务编排: 输入校验 -> 内核 -> 证据 -> 质量门。

关键语义:
- 内核 ``converged=False`` (迭代预算耗尽) => :class:`NonConvergenceError`,
  即使已经停止迭代也 **绝不返回成功**; 部分结果作为"不确定结论"单列。
- 收敛了但残差/正交性/重构证据超阈值 => :class:`QualityCheckError`。
- 全部通过才返回 :class:`DecompositionResult`。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import numpy as np

from .config import EigenConfig
from .errors import EigenserviceError, NonConvergenceError, QualityCheckError
from .evidence import (
    OrthogonalityEvidence,
    ReconstructionEvidence,
    ResidualEvidence,
    orthogonality_evidence,
    reconstruction_evidence,
    residual_evidence,
)
from .kernel import KernelResult, eigh_core
from .trace import RequestLog
from .validation import parse_matrix


@dataclass(frozen=True)
class DecompositionResult:
    eigenvalues: list[float]
    eigenvectors: list[list[float]]
    size: int
    sweeps: int
    quality: dict[str, Any]
    trace: dict[str, Any]


def _quality_dict(
    res: ResidualEvidence,
    ort: OrthogonalityEvidence,
    rec: ReconstructionEvidence,
) -> dict[str, Any]:
    return {
        "residual": {
            "relative_fro": res.relative_fro,
            "absolute_fro": res.absolute_fro,
            "max_per_pair_relative": res.max_per_pair_relative,
            "per_pair_relative": list(res.per_pair_relative),
        },
        "orthogonality": {
            "deviation_fro": ort.deviation_fro,
            "relative_deviation": ort.relative_deviation,
            "max_abs_deviation": ort.max_abs_deviation,
        },
        "reconstruction": {
            "relative_fro": rec.relative_fro,
            "absolute_fro": rec.absolute_fro,
        },
    }


def _evaluate_quality(
    matrix: np.ndarray, eigvals: np.ndarray, eigvecs: np.ndarray
) -> tuple[ResidualEvidence, OrthogonalityEvidence, ReconstructionEvidence]:
    return (
        residual_evidence(matrix, eigvals, eigvecs),
        orthogonality_evidence(eigvecs),
        reconstruction_evidence(matrix, eigvals, eigvecs),
    )


def decompose(
    payload: Any,
    config: EigenConfig | None = None,
    log: RequestLog | None = None,
) -> DecompositionResult:
    """执行一次完整特征分解 (成功路径)。失败以显式异常分类抛出。"""
    config = config or EigenConfig.from_env()
    log = log or RequestLog()
    log.step("request_received")

    try:
        matrix = parse_matrix(payload, config)
    except EigenserviceError as exc:
        # 输入类失败也要可解释: 记录原因并把 trace 挂到 details。
        log.fail(exc.code, exc.message, **exc.details)
        exc.details["trace"] = log.trace()
        raise
    n = matrix.shape[0]
    log.step("input_validated", size=n,
             relative_asymmetry_checked=config.sym_tol)

    max_sweeps = config.max_sweeps_for(n)
    log.step("kernel_start", core="householder+implicit-wilkinson-ql",
             max_sweeps=max_sweeps)
    kernel: KernelResult = eigh_core(
        matrix, max_sweeps=max_sweeps, eig_tol=config.eig_tol
    )
    log.step("kernel_finished", converged=kernel.converged,
             sweeps=kernel.sweeps, stalled_index=kernel.stalled_index)

    # 预算耗尽 => 显式失败, 不允许"停止即成功"。
    if not kernel.converged:
        partial_res = residual_evidence(
            matrix, kernel.eigenvalues, kernel.eigenvectors
        )
        log.fail(
            "not_converged",
            "迭代预算耗尽, 特征值未全部收敛, 结论不确定。",
            stalled_index=kernel.stalled_index,
            sweeps=kernel.sweeps,
            max_sweeps=max_sweeps,
            partial_relative_residual=partial_res.relative_fro,
        )
        log.uncertain(
            "迭代预算内未收敛",
            partial_eigenvalues=[float(x) for x in kernel.eigenvalues],
            partial_relative_residual=partial_res.relative_fro,
            stalled_index=kernel.stalled_index,
        )
        raise NonConvergenceError(
            "迭代预算耗尽, 特征分解未收敛; 已停止迭代但结果不可作为成功结论。",
            {
                "size": n,
                "sweeps": kernel.sweeps,
                "max_sweeps": max_sweeps,
                "stalled_index": kernel.stalled_index,
                "partial_relative_residual": partial_res.relative_fro,
                "trace": log.trace(),
            },
        )

    res, ort, rec = _evaluate_quality(
        matrix, kernel.eigenvalues, kernel.eigenvectors
    )
    log.step(
        "evidence_evaluated",
        relative_residual=res.relative_fro,
        orthogonality_deviation=ort.deviation_fro,
        relative_reconstruction=rec.relative_fro,
    )

    violations = _quality_violations(config, res, ort, rec)
    if violations:
        log.fail("quality_check_failed", "后验证据未达阈值。", **violations)
        log.uncertain(
            "数值证据超阈值, 结果可能不可靠",
            relative_residual=res.relative_fro,
            orthogonality_deviation=ort.deviation_fro,
            relative_reconstruction=rec.relative_fro,
        )
        raise QualityCheckError(
            "特征分解完成但残差/正交性/重构证据未达配置阈值。",
            {"violations": violations, "trace": log.trace()},
        )

    log.step("quality_gate_passed")
    return DecompositionResult(
        eigenvalues=[float(x) for x in kernel.eigenvalues],
        eigenvectors=[[float(x) for x in row]
                      for row in kernel.eigenvectors.T],
        size=n,
        sweeps=kernel.sweeps,
        quality=_quality_dict(res, ort, rec),
        trace=log.trace(),
    )


def _quality_violations(
    config: EigenConfig,
    res: ResidualEvidence,
    ort: OrthogonalityEvidence,
    rec: ReconstructionEvidence,
) -> dict[str, float]:
    violations: dict[str, float] = {}
    if res.relative_fro > config.residual_tol:
        violations["relative_residual"] = res.relative_fro
    if ort.deviation_fro > config.orthogonality_tol:
        violations["orthogonality_deviation"] = ort.deviation_fro
    if rec.relative_fro > config.reconstruction_tol:
        violations["relative_reconstruction"] = rec.relative_fro
    return violations
