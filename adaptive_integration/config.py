"""积分配置与输入范围约束。

数值容差、深度上限、求值次数上限均在此集中定义并校验，失败时抛出
IntegratorError，由 JSON 层翻译为明确的错误响应，绝不静默调整。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Sequence


class IntegratorError(ValueError):
    """配置或请求不合法（对应 JSON 错误码 INVALID_REQUEST）。"""


# 硬性范围（文档化的输入范围）
LIMITS = {
    "abs_tol": (1e-13, 1.0),
    "rel_tol": (0.0, 1.0),
    "max_depth": (1, 100),
    "max_evaluations": (15, 1_000_000),
    "initial_intervals": (1, 100000),
    "interval_width": (1e-300, 1e100),
    "bound_abs": 1e100,
    "expression_length": 500,
}


@dataclass(frozen=True)
class IntegrationConfig:
    """一次积分请求的全部参数。

    method:                'gk15'（Gauss-Kronrod 7-15，默认）或 'simpson'
    abs_tol / rel_tol:     全局误差预算 eps = abs_tol + rel_tol*|I|
    max_depth:             单个子区间允许的最大细分深度
    max_evaluations:       被积函数求值次数的硬上限（预算耗尽即失败）
    initial_intervals:     初始均匀子区间数（窄峰场景可增大）
    points:                额外的内部分点（如已知奇点/窄峰位置）
    """

    method: str = "gk15"
    abs_tol: float = 1e-10
    rel_tol: float = 1e-8
    max_depth: int = 60
    max_evaluations: int = 100_000
    initial_intervals: int = 1
    points: Optional[Sequence[float]] = None

    @staticmethod
    def _finite_number(v, name: str) -> float:
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            raise IntegratorError(f"{name} 必须是数值")
        v = float(v)
        if not (v == v) or v in (float("inf"), float("-inf")):
            raise IntegratorError(f"{name} 必须是有限数值")
        return v

    @classmethod
    def validate_request(
        cls,
        expression,
        a,
        b,
        method="gk15",
        abs_tol=1e-10,
        rel_tol=1e-8,
        max_depth=60,
        max_evaluations=100_000,
        initial_intervals=1,
        points=None,
    ) -> "IntegrationConfig":
        """对来自 JSON 的原始参数做完整校验，返回不可变配置。"""

        if not isinstance(expression, str) or not expression.strip():
            raise IntegratorError("expression 必须是非空字符串")
        if len(expression) > LIMITS["expression_length"]:
            raise IntegratorError(
                f"expression 过长（上限 {LIMITS['expression_length']} 字符）"
            )

        if method not in ("gk15", "simpson"):
            raise IntegratorError(
                f"method 必须是 'gk15' 或 'simpson'，收到 {method!r}"
            )

        af = cls._finite_number(a, "a")
        bf = cls._finite_number(b, "b")
        if abs(af) > LIMITS["bound_abs"] or abs(bf) > LIMITS["bound_abs"]:
            raise IntegratorError(
                f"积分端点绝对值不得超过 {LIMITS['bound_abs']:.0e}"
            )
        width = abs(bf - af)
        if width != 0.0:
            if width < LIMITS["interval_width"][0]:
                raise IntegratorError(
                    "积分区间过窄，下限 "
                    f"{LIMITS['interval_width'][0]:.0e}"
                )
            if width > LIMITS["interval_width"][1]:
                raise IntegratorError(
                    f"积分区间过宽，上限 {LIMITS['interval_width'][1]:.0e}"
                )

        abs_tol_f = cls._finite_number(abs_tol, "abs_tol")
        rel_tol_f = cls._finite_number(rel_tol, "rel_tol")
        lo, hi = LIMITS["abs_tol"]
        if not (lo <= abs_tol_f <= hi):
            raise IntegratorError(f"abs_tol 必须落在 [{lo:g}, {hi:g}]")
        rlo, rhi = LIMITS["rel_tol"]
        if not (rlo <= rel_tol_f <= rhi):
            raise IntegratorError(f"rel_tol 必须落在 [{rlo:g}, {rhi:g}]")
        if abs_tol_f == lo and rel_tol_f == 0.0:
            # 机器精度量级的目标无法可靠判定，明确拒绝
            raise IntegratorError(
                "abs_tol 已为下限 1e-13 且 rel_tol=0："
                "目标容差接近机器精度，无法保证判敛，请放宽容差"
            )

        def _int_param(v, name, lo, hi):
            if isinstance(v, bool) or not isinstance(v, int):
                # 容忍 JSON 里的 60.0，但拒绝 1.5
                if isinstance(v, float) and v.is_integer():
                    v = int(v)
                else:
                    raise IntegratorError(f"{name} 必须是整数")
            if not (lo <= v <= hi):
                raise IntegratorError(f"{name} 必须落在 [{lo}, {hi}]")
            return v

        dlo, dhi = LIMITS["max_depth"]
        max_depth_i = _int_param(max_depth, "max_depth", dlo, dhi)
        elo, ehi = LIMITS["max_evaluations"]
        max_eval_i = _int_param(
            max_evaluations, "max_evaluations", elo, ehi
        )
        ilo, ihi = LIMITS["initial_intervals"]
        initial_i = _int_param(
            initial_intervals, "initial_intervals", ilo, ihi
        )

        checked_points = None
        if points is not None:
            if not isinstance(points, (list, tuple)):
                raise IntegratorError("points 必须是数值数组")
            if len(points) > 100:
                raise IntegratorError("points 最多 100 个")
            checked_points = []
            for p in points:
                pf = cls._finite_number(p, "points[]")
                if not (min(af, bf) < pf < max(af, bf)):
                    raise IntegratorError(
                        f"分点 {pf:g} 不在开区间 ({af:g}, {bf:g}) 内"
                    )
                checked_points.append(pf)
            if len(set(checked_points)) != len(checked_points):
                raise IntegratorError("points 含有重复分点")
            checked_points.sort()

        return cls(
            method=method,
            abs_tol=abs_tol_f,
            rel_tol=rel_tol_f,
            max_depth=max_depth_i,
            max_evaluations=max_eval_i,
            initial_intervals=initial_i,
            points=checked_points,
        )
