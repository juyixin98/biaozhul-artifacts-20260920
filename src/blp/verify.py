"""独立于单纯形表的结果核验。

最优性/无界性核验在**原始规范系统**（``A_le y <= b_le``、
``A_eq y = b_eq``、``0 <= y <= ub``，变量已平移）上直接做；
不可行证书核验复用 :mod:`blp.model` 的标准化构造，保证证书与
核验建立在同一份标准形上。
"""

from __future__ import annotations

import numpy as np

from .model import TOL, LP, build_standard_form


def solution_residuals(
    x,
    A_le, b_le,
    A_eq, b_eq,
    ub,
) -> dict:
    """计算最优解各部分残差与最大违约量（``<=`` 正值即违约）。"""
    x = np.asarray(x, dtype=float).ravel()
    out: dict = {}

    if A_le is not None and A_le.shape[0]:
        viol = A_le @ x - b_le
        out["ineq"] = {
            "max_violation": float(np.max(viol)),
            "max_abs_violation": float(np.max(np.abs(viol))),
            "per_row": [float(v) for v in viol],
        }
    else:
        out["ineq"] = {"max_violation": 0.0, "max_abs_violation": 0.0,
                       "per_row": []}

    if A_eq is not None and A_eq.shape[0]:
        resid = A_eq @ x - b_eq
        out["eq"] = {
            "max_abs_residual": float(np.max(np.abs(resid))),
            "per_row": [float(v) for v in resid],
        }
    else:
        out["eq"] = {"max_abs_residual": 0.0, "per_row": []}

    low_viol = float(np.min(x)) if x.size else 0.0
    out["nonnegativity"] = {"min_x": low_viol}

    ub = np.asarray(ub, dtype=float).ravel()
    finite = np.isfinite(ub)
    if np.any(finite):
        ub_viol = x[finite] - ub[finite]
        out["upper_bounds"] = {
            "max_violation": float(np.max(ub_viol)),
            "max_abs_violation": float(np.max(np.abs(ub_viol))),
        }
    else:
        out["upper_bounds"] = {"max_violation": 0.0, "max_abs_violation": 0.0}

    worst = max(
        out["ineq"]["max_violation"],
        out["eq"]["max_abs_residual"],
        max(0.0, -low_viol),
        out["upper_bounds"]["max_violation"],
    )
    out["overall_max_violation"] = float(worst)
    out["feasible"] = bool(worst <= TOL.feas)
    return out


def ray_residuals(x0, d, A_le, b_le, A_eq, b_eq, ub) -> dict:
    """核验无界射线 ``x(t) = x0 + t d``（t >= 0）。

    条件：``x0`` 可行；``d >= 0``；``A_le d <= 0``；``A_eq d = 0``；
    有有限上界的分量 ``d_j = 0``。
    """
    x0 = np.asarray(x0, dtype=float).ravel()
    d = np.asarray(d, dtype=float).ravel()
    base = solution_residuals(x0, A_le, b_le, A_eq, b_eq, ub)

    out: dict = {"x0_feasible": base}
    out["direction_min_component"] = float(np.min(d))
    out["ineq_direction_max"] = (
        float(np.max(A_le @ d)) if A_le is not None and A_le.shape[0] else 0.0
    )
    out["eq_direction_max_abs"] = (
        float(np.max(np.abs(A_eq @ d)))
        if A_eq is not None and A_eq.shape[0]
        else 0.0
    )
    ub = np.asarray(ub, dtype=float).ravel()
    finite = np.isfinite(ub)
    out["bound_direction_max"] = (
        float(np.max(d[finite])) if np.any(finite) else 0.0
    )
    worst = max(
        max(0.0, out["ineq_direction_max"]),
        out["eq_direction_max_abs"],
        max(0.0, -out["direction_min_component"]),
        max(0.0, out["bound_direction_max"]),
    )
    out["overall_max_violation"] = float(worst)
    out["valid"] = bool(worst <= TOL.feas and base["feasible"])
    return out


def verify_certificate(
    certificate_rows,
    A_le, b_le, A_eq, b_eq, ub,
) -> dict:
    """独立核验原问题的 Farkas 不可行证书。

    证书给出每行乘子 λ：``<=`` 行（含上界行）乘子必须 >= 0，等式行
    自由。条件：

      * ``lambda^T A_struct >= 0``（每个结构变量列）；
      * 每个松弛列对应行的乘子非负（已含在上一条的行类型检查里）；
      * ``lambda^T bbar < 0``（bbar 为标准化右端，取反行已乘 -1）。

    本函数重建与求解器完全相同的标准形，再按原始 Abar/bbar 计算。
    """
    n = (A_le.shape[1] if A_le is not None else
         A_eq.shape[1] if A_eq is not None else ub.shape[0])
    lp = LP(
        c=np.zeros(n),
        A_ub=A_le, b_ub=b_le,
        A_eq=A_eq, b_eq=b_eq,
        ub=ub.copy(),
    )
    lp.shift = np.zeros(n)
    lp.lb_original = np.zeros(n)
    # 传入的 ub 就是平移后的有效界，与求解器内部口径一致。
    lp.ub_original = ub
    lp.fixed = {}
    sf = build_standard_form(lp)

    u = np.zeros(sf.Abar.shape[0])
    by_kind = {}
    for r, (kind, idx, sgn) in enumerate(sf.row_kinds):
        by_kind[(kind, idx)] = r
    for entry in certificate_rows:
        key = (entry["kind"], entry["index"])
        if key not in by_kind:
            raise ValueError(f"证书引用了不存在的标准行 {key}")
        u[by_kind[key]] = entry["multiplier"]

    # 证书给的是原始行乘子 mu；翻转行回到标准化坐标时 lam = sgn*mu。
    signs = np.array([sgn for _, _, sgn in sf.row_kinds], dtype=float)
    lam = signs * u

    # 原始 <= 行（含上界行）乘子非负；等式行自由。
    ineq_rows = np.array(
        [r for r, (k, _, _) in enumerate(sf.row_kinds) if k != "eq"],
        dtype=int,
    )
    min_ineq = float(np.min(u[ineq_rows])) if ineq_rows.size else 0.0

    # 在原始行结构列上：mu^T A_orig >= 0。
    struct = np.arange(sf.n_orig)
    A_orig = sf.Abar[:, struct] * signs[:, None]
    Atmu = A_orig.T @ u if struct.size else np.zeros(0)
    min_struct = float(np.min(Atmu)) if Atmu.size else 0.0

    # 矛盾常数：标准形上 lam^T bbar（翻转行两侧同取反，与原始口径相等）。
    ltb = float(lam @ sf.bbar)
    return {
        "min_ineq_multiplier": min_ineq,
        "min_struct_Atmu": min_struct,
        "muTb": ltb,
        "valid": bool(
            min_ineq >= -TOL.cert
            and min_struct >= -TOL.cert
            and ltb < -TOL.cert
        ),
    }
