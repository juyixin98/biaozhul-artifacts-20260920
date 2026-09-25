"""数值容差与问题规模限制。

求解器为**小中规模、教学级**稠密单纯形实现，所有浮点判定均基于显式容差。
三组容差各司其职，避免用同一个 epsilon 处理不同量级的判定：

================  =================  ==========================================
容差               默认值             用途
================  =================  ==========================================
``feas_tol``      1e-7               可行性：约束残差、变量下界、Phase I 最优值
``pivot_tol``     1e-10              枢轴列/枢轴元判定（接近零的矩阵元素）
``reduced_tol``   1e-8               既约费用符号（最优性检验）
================  =================  ==========================================

容差均为**绝对容差**，因此输入系数请保持在合理量级（见 :data:`MAX_ABS_VALUE`）。
问题规模限制见 :data:`MAX_VARIABLES` / :data:`MAX_CONSTRAINTS`。
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class Tolerance:
    """数值容差集合。"""

    feas_tol: float = 1e-7
    pivot_tol: float = 1e-10
    reduced_tol: float = 1e-8

    def __post_init__(self) -> None:
        for name in ("feas_tol", "pivot_tol", "reduced_tol"):
            value = getattr(self, name)
            if not (0.0 < float(value) < 1.0):
                raise ValueError(f"{name} 必须位于 (0, 1)，收到 {value!r}")
        if not (self.pivot_tol < self.reduced_tol < self.feas_tol):
            raise ValueError(
                "容差需满足 pivot_tol < reduced_tol < feas_tol，"
                f"收到 {self.pivot_tol}, {self.reduced_tol}, {self.feas_tol}"
            )


# ---- 输入范围（在 problem 层强制校验）-------------------------------------
MAX_VARIABLES = 300
"""决策变量数上限（标准形总列数可能因松弛/人工变量更多）。"""

MAX_CONSTRAINTS = 300
"""不等式 + 等式约束总数上限。"""

MAX_ABS_VALUE = 1e6
"""单个输入系数/右端项绝对值的建议上限。"""

MAX_ITERATIONS = 10_000
"""两阶段合计主元迭代上限；超过返回/抛出 failed: iteration_limit。"""

DEFAULT_TOLERANCE = Tolerance()
