"""高精度独立参考实现。

**独立性保证**：本模块只调用 mpmath（任意精度，算法为 mpmath 自带的
Durand-Kerner/Weierstrass 或 Aberth 实现），不导入 polyroots.kernels /
polyroots.evidence。测试参考答案由此产生，被测核心不许自己生成答案。

返回的根经 mpmath 高精度独立复验（逐根残差 + 由根重构系数），并与显式
已知根（合成夹具）逐一匹配，匹配采用全局最近邻贪心，不做“按位置对应”。
"""

from __future__ import annotations

import dataclasses

import mpmath as mp


@dataclasses.dataclass(frozen=True)
class HighPrecisionResult:
    roots: list[complex]                # 高精度根（转 float64 仅用于比较输出）
    roots_mp: list                      # mpmath 原始高精度根，供严格比较
    dps: int
    max_residual: float                 # 高精度下 |p(z)| / S(z)
    max_factor_error: float             # 高精度重构误差


def _to_mp(c: complex) -> mp.mpc:
    return mp.mpc(mp.mpf(c.real), mp.mpf(c.imag))


def high_precision_roots(
    coeffs_asc: list[complex],
    dps: int = 60,
    maxsteps: int = 2000,
    extra_prec: int = 40,
) -> HighPrecisionResult:
    """coeffs_asc 为升序（常数项在前）复系数；用 mpmath.polyroots 独立求解。

    传 asc=True（升序），mpmath 内部为 Durand-Kerner/Weierstrass 同时迭代，
    与本仓库 companion(LAPACK) 及自带 Aberth 实现是独立算法；
    error=True 返回逐根误差估计。
    """
    mp.mp.dps = dps + extra_prec
    asc = [_to_mp(c) for c in coeffs_asc]

    try:
        roots, errors = mp.polyroots(
            asc, maxsteps=maxsteps, extraprec=extra_prec, error=True, asc=True
        )
    except Exception as exc:  # 高精度不收敛等：报告为计算失败，由测试侧感知
        raise RuntimeError(f"mpmath 高精度参考求解失败: {exc}") from exc

    max_res = _max_relative_residual(asc, roots)
    max_fac = _max_factor_error(asc, roots)
    return HighPrecisionResult(
        roots=[complex(r.real, r.imag) for r in roots],
        roots_mp=list(roots),
        dps=dps,
        max_residual=float(max_res),
        max_factor_error=float(max_fac),
    )


def _eval_mp(asc: list[mp.mpc], z: mp.mpc) -> mp.mpc:
    """升序系数求值：f(z)=Σ a_k z^k，从最高次向下 Horner。"""
    val = mp.mpf("0")
    for a in reversed(asc):
        val = val * z + a
    return val


def _max_relative_residual(asc: list[mp.mpc], roots: list[mp.mpc]) -> mp.mpf:
    worst = mp.mpf("0")
    for z in roots:
        fval = _eval_mp(asc, z)
        scale = mp.mpf("0")
        power = mp.mpf("1")
        az = abs(z)
        for a in asc:  # 升序：第 k 项 |a_k| |z|^k
            scale += abs(a) * power
            power *= az
        res = abs(fval) / scale if scale > 0 else abs(fval)
        worst = max(worst, res)
    return worst


def _max_factor_error(asc: list[mp.mpc], roots: list[mp.mpc]) -> mp.mpf:
    """由参考根重构首一多项式（升序），与首一化输入（升序）比较。"""
    an = asc[-1]
    input_monic_asc = [a / an for a in asc]  # [c0/an, ..., 1]
    poly = [mp.mpf("1")]  # 升序，初始常数多项式 1
    for z in roots:
        poly = _conv(poly, [-z, mp.mpf("1")])  # 乘升序因子 (x-z)=[-z, 1]
    worst = mp.mpf("0")
    for got, want in zip(poly, input_monic_asc):
        worst = max(worst, abs(got - want) / (1 + abs(want)))
    return worst


def _conv(a: list, b: list) -> list:
    out = [mp.mpf("0")] * (len(a) + len(b) - 1)
    for i, x in enumerate(a):
        for j, y in enumerate(b):
            out[i + j] += x * y
    return out


def match_roots(
    candidate: list[complex], reference: list[complex]
) -> list[tuple[int, int, float]]:
    """全局最近邻贪心匹配，返回 (候选下标, 参考下标, 相对距离)，按距离排序。"""
    pairs = []
    for i, a in enumerate(candidate):
        for j, b in enumerate(reference):
            d = abs(a - b) / (1.0 + 0.5 * (abs(a) + abs(b)))
            pairs.append((d, i, j))
    pairs.sort()
    used_a: set[int] = set()
    used_b: set[int] = set()
    matches = []
    for d, i, j in pairs:
        if i in used_a or j in used_b:
            continue
        used_a.add(i)
        used_b.add(j)
        matches.append((i, j, d))
    return matches
