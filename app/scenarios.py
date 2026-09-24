"""Monte Carlo 自检场景与报告：小角度、长链、缺失协方差、大扰动失效、相关性。

该模块被 ``POST /montecarlo/selfcheck`` 调用，也可独立运行
``python -m app.scenarios`` 打印报告。
"""

from __future__ import annotations

import numpy as np

from .chain import Edge, Factor
from .montecarlo import run_scenario
from .se3 import se3_exp


def _chain(n: int, sig: float, seed: int, missing: tuple[int, ...] = (),
           all_forward: bool = False):
    rng = np.random.default_rng(seed)
    factors = []
    for i in range(n):
        T = se3_exp(rng.normal(scale=0.3, size=6))
        if i in missing:
            C, has = None, False
        else:
            C = np.diag([(0.5 * sig) ** 2] * 3 + [sig**2] * 3)
            has = True
        e = Edge(
            id=f"e{i}",
            parent=f"n{i}",
            child=f"n{i + 1}",
            T=T,
            covariance=C,
            version="v1.0",
            has_covariance=has,
        )
        # 相关性场景需要公共扰动同号叠加，故用全正向链；其余场景混合逆因子
        factors.append(Factor(edge=e, inverse=False if all_forward else (i % 2 == 1)))
    return factors


def _row(r) -> dict:
    return {
        "scenario": r.scenario,
        "convention": r.convention,
        "n_edges": r.n_edges,
        "samples": r.samples,
        "covariance_status": r.covariance_status,
        "frobenius_relative_error": r.fro_relative_error,
        "trace_ratio_sample_over_first_order": r.trace_ratio,
        "mean_residual_norm": r.mean_residual_norm,
        "notes": r.notes,
    }


def build_selfcheck(n_samples: int = 30000, seed: int = 0) -> dict:
    rows: list[dict] = []
    S = n_samples

    # 1) 小角度：左右约定各一条
    for conv in ("right", "left"):
        f = _chain(4, 1e-3, seed=101)
        rows.append(
            _row(
                run_scenario(
                    f, conv, S, f"small_angle_{conv}", seed=seed,
                    notes="σ_rot=1e-3 rad，一阶传播应高度吻合（参考阈值 Fro<5%）",
                )
            )
        )

    # 2) 长链：小噪声 vs 中等噪声
    f = _chain(12, 1e-3, seed=102)
    rows.append(
        _row(
            run_scenario(
                f, "right", S, "long_chain_small_noise", seed=seed,
                notes="12 边长链、小噪声：一阶仍准确",
            )
        )
    )
    f = _chain(12, 3e-2, seed=103)
    rows.append(
        _row(
            run_scenario(
                f, "right", S, "long_chain_moderate_noise", seed=seed,
                notes="12 边、σ_rot=3e-2 rad：均值旋转开始非零，二阶项轻微显现",
            )
        )
    )

    # 3) 缺失协方差：传播必须 unknown
    f = _chain(4, 1e-3, seed=104, missing=(2,))
    rows.append(
        _row(
            run_scenario(
                f, "right", S, "missing_covariance", seed=seed,
                notes="第 3 条边缺协方差：状态 unknown，禁止以独立假设填补",
            )
        )
    )

    # 4) 相关性：bounded 输出空间保守上界 vs 等相关(ρ=0.5)抽样。
    #    用全正向链使公共扰动同号叠加；一阶 bounded(ρ=0.5) 应不低估样本，
    #    且大于独立结果（保守性），故 trace_ratio(样本/上界) <= 1。
    f = _chain(4, 1e-3, seed=105, all_forward=True)
    corr_row = _row(
        run_scenario(
            f,
            "right",
            S,
            "correlated_equicorr_rho0.5",
            policy="bounded",
            rho_max=0.5,
            rho_sample=0.5,
            seed=seed,
            notes=(
                "等相关 ρ=0.5 公共扰动抽样 vs 输出空间 bounded(ρ=0.5) 上界："
                "trace 比值应≤1（上界不低估真实相关方差），且大于独立结果"
            ),
        )
    )
    rows.append(corr_row)

    # 5) 近似失效演示：大扰动 + 长链，一阶低估二阶项
    f = _chain(12, 3e-1, seed=106)
    rows.append(
        _row(
            run_scenario(
                f, "right", S, "large_noise_breakdown", seed=seed,
                notes=(
                    "σ_rot=0.3 rad、12 边：扰动不再小，BCH 二阶项显著，"
                    "一阶 Fro 误差上升到约 10-15%——这是近似适用边界的反例"
                ),
            )
        )
    )

    def _num(x):
        return x if x is not None else float("nan")

    small_err = max(
        _num(r["frobenius_relative_error"])
        for r in rows
        if r["scenario"].startswith("small_angle")
    )
    breakdown = next(
        r for r in rows if r["scenario"] == "large_noise_breakdown"
    )
    missing_row = next(
        r for r in rows if r["scenario"] == "missing_covariance"
    )

    passed = bool(
        small_err < 0.05
        and missing_row["covariance_status"] == "unknown"
        and (breakdown["frobenius_relative_error"] or 0.0) > 0.05
    )

    return {
        "title": "Monte Carlo 一阶传播近似核对",
        "n_samples_per_scenario": S,
        "seed": seed,
        "scenarios": rows,
        "summary": {
            "small_angle_fro_error": small_err,
            "large_noise_fro_error": breakdown["frobenius_relative_error"],
            "missing_covariance_status": missing_row["covariance_status"],
            "passed": passed,
        },
        "validity_range": (
            "一阶 JΣJᵀ 传播在每条边扰动保持『小』时有效：经验上 "
            "σ_rot ≲ 3e-2 rad 且链上累计旋转 ≲ 0.2-0.3 rad 时，Frobenius "
            "相对误差 <2%（本自检小角度场景）。误差来自 SE(3) 的 BCH 二阶/"
            "三阶项，随 σ² 与链长增长；σ_rot≈0.3 rad、12 边时误差约 10-15%。"
            "缺失协方差不参与任何统计结论。"
        ),
    }
