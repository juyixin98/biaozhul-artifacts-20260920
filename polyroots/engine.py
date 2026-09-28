"""编排层：把输入边界、计算内核、误差证据、排序契约、运行日志串成一次求解。

状态判定原则（防“边界输入悄悄算错”的关键）：
- Aberth 任一单根未达 tol            -> status=not_converged，根 kind=unconverged；
- 近重根无论残差多小                 -> kind=near_repeated 且给出簇间距与敏感性；
- companion 无逐根收敛信息           -> 逐根 converged 仅表示“LAPACK 已返回”，
                                       可信度以 factor_error/vieta/κ 证据为准，
                                       factor_error 偏大时产生显式 warning。
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any, Sequence

import numpy as np

from . import evidence
from .config import DEFAULT_LOG_DIR
from .errors import ComputationFailedError
from .kernels import run_kernel
from .models import (
    CoeffOrder,
    KernelName,
    RootEvidence,
    RootKind,
    RootRecord,
    SolveOptions,
    SolveResult,
    SolveStatus,
)
from .ordering import conjugate_pairs, stable_order
from .runlog import RunStore, input_fingerprint
from .validation import parse_and_normalize

_FACTOR_WARN_LEVEL = 1e-7
_ZERO_ROOT_TOL = 1e-12


def new_run_id() -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    return f"run-{stamp}-{uuid.uuid4().hex[:8]}"


def solve_polynomial(
    coefficients: Sequence[Any],
    order: CoeffOrder | str = CoeffOrder.DESCENDING,
    options: SolveOptions | None = None,
    run_id: str | None = None,
    store: RunStore | None = None,
) -> SolveResult:
    opts = options or SolveOptions()
    opts.validate()
    order = CoeffOrder(order)
    run_id = run_id or new_run_id()

    norm_asc, stripped = parse_and_normalize(coefficients, order, opts.max_degree)
    fp = input_fingerprint(
        [[c.real, c.imag] for c in norm_asc.tolist()],
        dataclasses_options(opts),
    )
    if store is not None:
        store.register_or_conflict(run_id, fp)

    kernel_name = opts.kernel
    kout, used_kernel = run_kernel(kernel_name, norm_asc,
                                   opts.max_iterations, opts.convergence_tol, opts.seed)
    kernel_name = used_kernel

    roots = np.asarray(kout.roots, dtype=np.complex128)
    if roots.shape != (norm_asc.size - 1,):
        raise ComputationFailedError(
            "内核返回根数量与次数不一致",
            {"expected": norm_asc.size - 1, "got": int(roots.size)},
        )

    residuals = np.array([evidence.relative_residual(norm_asc, z) for z in roots])
    clusters = evidence.cluster_roots(roots, opts.cluster_tol)
    separations = evidence.nearest_separation(roots)
    kappas = evidence.sensitivity(norm_asc, roots)
    fac = evidence.factor_error(norm_asc, roots)
    vieta = evidence.vieta_check(norm_asc, roots)

    order_idx = stable_order(roots)
    pairs = conjugate_pairs(roots, order_idx, opts.conjugate_tol)

    cluster_id_of = np.full(roots.size, -1, dtype=int)
    for cid, members in enumerate(clusters):
        for m in members:
            cluster_id_of[m] = cid

    root_radius = float(np.max(np.abs(roots))) if roots.size else 1.0
    warnings: list[str] = []
    records: list[RootRecord] = []
    for sorted_pos, orig in enumerate(order_idx):
        rec = _build_root_record(
            orig=orig,
            sorted_pos=sorted_pos,
            value=roots[orig],
            residual=float(residuals[orig]),
            cluster_id=int(cluster_id_of[orig]),
            cluster_size=len(clusters[cluster_id_of[orig]])
            if cluster_id_of[orig] >= 0 else 1,
            separation=float(separations[orig]),
            kappa=float(kappas[orig]),
            converged=bool(kout.converged[orig]),
            iterations=None if kout.per_root_iterations[orig] == 0
            and kernel_name is KernelName.COMPANION
            else int(kout.per_root_iterations[orig]),
            pair_sorted_pos=pairs[sorted_pos],
            zero_tol=_ZERO_ROOT_TOL * max(1.0, root_radius),
            kernel_name=kernel_name,
            tol=opts.convergence_tol,
        )
        records.append(rec)

    unconverged = [i for i in range(roots.size) if not kout.converged[i]]
    if unconverged:
        warnings.append(
            f"{len(unconverged)} 个根在 {kout.iterations_used} 轮内未达到 "
            f"tol={opts.convergence_tol:g}，状态保留为 unconverged；"
            "其残差即使很小也不得解读为准确根"
        )
    for cid, members in enumerate(clusters):
        warnings.append(
            f"检测到 {len(members)} 近重根簇 cluster_id={cid}，最小相对间距 "
            f"{float(np.min(separations[members])):.3e}，最大敏感性 κ="
            f"{float(np.max(kappas[members])):.3e}；该簇根对系数扰动敏感，"
            "小残差不构成精度保证"
        )
    if fac.max_rel_coeff_error > _FACTOR_WARN_LEVEL:
        warnings.append(
            f"因子重构最大相对系数误差 {fac.max_rel_coeff_error:.3e}（包络归一化）"
            f"超过 {_FACTOR_WARN_LEVEL:.0e}，根集合整体精度可疑（常见于近重根）"
        )
    elif fac.strict_float64_error > _FACTOR_WARN_LEVEL:
        if fac.high_precision_error is not None and fac.high_precision_error <= 1e-9:
            warnings.append(
                f"float64 严格重构误差 {fac.strict_float64_error:.2e} 较大，但该多项式有 "
                f"{fac.zero_coeff_count} 个（近）零系数，消去比 "
                f"{fac.cancellation_ratio:.1e}；mpmath 高精度独立重构误差仅 "
                f"{fac.high_precision_error:.2e}，判定大误差为乘积消去假象而非根错误"
            )
        else:
            warnings.append(
                f"因子重构严格误差 {fac.strict_float64_error:.2e} 较大且高精度复核"
                f"({fac.high_precision_error}) 未消除，根集合精度可疑"
            )
    if stripped:
        warnings.append(
            f"已剥离 {stripped} 个零首项（输入最高次存在零系数），"
            f"实际求解次数为 {norm_asc.size - 1}"
        )

    status = (SolveStatus.CONVERGED if not unconverged
              else SolveStatus.NOT_CONVERGED)

    from .models import KernelReport
    kreport = KernelReport(
        name=kernel_name.value,
        iterations_used=kout.iterations_used,
        converged_count=int(np.sum(kout.converged)),
        unconverged_count=len(unconverged),
        intermediate=kout.intermediate,
    )

    result = SolveResult(
        run_id=run_id,
        status=status,
        degree=norm_asc.size - 1,
        normalized_coeffs_asc=norm_asc,
        roots=records,
        factor_error=fac,
        vieta=vieta,
        kernel=kreport,
        options=opts,
        warnings=warnings,
    )

    if store is not None:
        store.write_run(_run_record(result, norm_asc, fp, stripped))
    return result


def _build_root_record(
    *, orig: int, sorted_pos: int, value: complex, residual: float,
    cluster_id: int, cluster_size: int, separation: float, kappa: float,
    converged: bool, iterations: int | None, pair_sorted_pos: int | None,
    zero_tol: float,
    kernel_name: KernelName, tol: float,
) -> RootRecord:
    if not converged:
        kind = RootKind.UNCONVERGED
        note = (f"Aberth 迭代耗尽/停滞，未达到 tol={tol:g}；"
                f"相对残差={residual:.2e} 仅说明当前点接近零曲线，"
                "近重根下不能据此声称根准确")
    elif cluster_id >= 0:
        kind = RootKind.NEAR_REPEATED
        note = (f"位于 {cluster_size} 近重根簇，最近邻相对间距={separation:.2e}，"
                f"敏感性 κ={kappa:.2e}；根误差可被系数扰动放大约 κ 倍，"
                "残差再小也需结合簇间距判断")
    elif abs(value) <= zero_tol:
        kind = RootKind.ZERO
        note = f"数值零根（|z|={abs(value):.2e}）"
    else:
        kind = RootKind.SIMPLE
        note = "孤立单根，残差与敏感性均常规"

    if kernel_name is KernelName.COMPANION and converged:
        note += "（companion 内核不提供逐根收敛信息，结论依据整体证据）"

    # 记录按规范顺序构建，conjugate_of 即配对根在规范顺序中的下标
    conjugate_of = pair_sorted_pos

    ev = RootEvidence(
        index=sorted_pos,
        relative_residual=residual,
        cluster_id=cluster_id,
        cluster_size=cluster_size,
        cluster_separation=separation,
        sensitivity_indicator=kappa,
        converged=converged,
        iterations=iterations,
        note=note,
    )
    return RootRecord(value=value, kind=kind,
                      conjugate_of=conjugate_of, evidence=ev)


def _run_record(result: SolveResult, norm_asc: np.ndarray,
                fingerprint: str, stripped: int) -> dict[str, Any]:
    return {
        "run_id": result.run_id,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "fingerprint": fingerprint,
        "status": result.status.value,
        "degree": result.degree,
        "stripped_zero_leaders": stripped,
        "normalized_coeffs_asc": [[c.real, c.imag] for c in norm_asc.tolist()],
        "options": dataclasses_options(result.options),
        "kernel": {
            "name": result.kernel.name,
            "iterations_used": result.kernel.iterations_used,
            "converged_count": result.kernel.converged_count,
            "unconverged_count": result.kernel.unconverged_count,
            "intermediate": result.kernel.intermediate,
        },
        "factor_error": {
            "max_rel_coeff_error": result.factor_error.max_rel_coeff_error,
            "rms_rel_coeff_error": result.factor_error.rms_rel_coeff_error,
            "constant_term_error": result.factor_error.constant_term_error,
            "strict_float64_error": result.factor_error.strict_float64_error,
            "high_precision_error": result.factor_error.high_precision_error,
            "cancellation_ratio": result.factor_error.cancellation_ratio,
            "zero_coeff_count": result.factor_error.zero_coeff_count,
            "reconstructed_coeffs_asc": [
                [c.real, c.imag]
                for c in result.factor_error.reconstructed_coeffs_asc
            ],
        },
        "vieta": {
            "sum_rel_error": result.vieta.sum_rel_error,
            "product_rel_error": result.vieta.product_rel_error,
        },
        "roots": [r.to_dict() for r in result.roots],
        "warnings": result.warnings,
    }


def dataclasses_options(opts: SolveOptions) -> dict[str, Any]:
    return {
        "kernel": opts.kernel.value,
        "max_iterations": opts.max_iterations,
        "convergence_tol": opts.convergence_tol,
        "cluster_tol": opts.cluster_tol,
        "conjugate_tol": opts.conjugate_tol,
        "max_degree": opts.max_degree,
        "seed": opts.seed,
    }


def get_default_store() -> RunStore:
    return RunStore(DEFAULT_LOG_DIR)
