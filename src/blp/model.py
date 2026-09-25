"""线性规划问题的数据模型与标准化。

支持的原始问题形式（非负变量）::

    最小化 (或最大化)  c^T x + c0
    s.t. A_op x  sense  b_op          sense ∈ {"<=", ">=", "="}
         lb <= x <= ub               lb >= 0（非负变量）
         部分变量可固定 x_j = f

实现范围限定为小中规模（默认上限见下方常量），超出直接拒绝，
不声称对大规模稀疏问题有良好性能。

数值容差全部集中在 :data:`TOL`，任何"等于零 / 可行 / 最优"判断
都通过这里的容差完成，避免各处使用魔数。
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from .errors import LPInputError

# ---------------------------------------------------------------------------
# 规模上限（"小中规模"的明确界定）
# ---------------------------------------------------------------------------
MAX_VARIABLES = 200
MAX_CONSTRAINTS = 200
# 标准化（松弛/人工列之后）的总列数上限
MAX_COLUMNS = 1_000
# 单阶段单纯形迭代次数上限
MAX_ITERATIONS = 10_000
# 输入系数的绝对值上限：防止极端数值导致舍入失效
MAX_ABS_COEFF = 1.0e9

# ---------------------------------------------------------------------------
# 数值容差
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Tolerances:
    """数值容差集合。

    - ``zero``: 判定"结构为零"（系数列、基矩阵元素）的绝对容差。
    - ``pivot``: 枢轴元素绝对值下限，低于则认为数值上奇异。
    - ``feas``: 可行性容差（原始约束残差、右端项）。
    - ``reduced``: 检验数最优性容差（绝对部分）。
    - ``rhs_negative``: 右端项允许为负的程度，超过说明数值崩溃。
    - ``cert``: 不可行证书核验容差（相对）。
    """

    zero: float = 1.0e-12
    pivot: float = 1.0e-10
    feas: float = 1.0e-8
    reduced: float = 1.0e-8
    rhs_negative: float = 1.0e-7
    cert: float = 1.0e-7


TOL = Tolerances()


# ---------------------------------------------------------------------------
# 输入模型
# ---------------------------------------------------------------------------


@dataclass
class LP:
    """一个线性规划实例。

    属性
    ------
    c:
        目标系数向量（长度 n）。
    sense:
        ``"min"`` 或 ``"max"``；内部统一按最小化处理。
    c0:
        目标常数项。
    A_ub, b_ub:
        不等式约束 ``A_ub x <= b_ub``（">"=" 由调用方取反后送入）。
    A_eq, b_eq:
        等式约束 ``A_eq x = b_eq``。
    ub:
        显式上界（长度 n），``None`` 元素表示 +∞；长度不足按 +∞ 补。
    fixed:
        ``{变量下标: 固定值}``，由 ``lb == ub`` 的变量产生。
    n:
        变量个数。
    """

    c: np.ndarray
    sense: str = "min"
    c0: float = 0.0
    A_ub: np.ndarray | None = None
    b_ub: np.ndarray | None = None
    A_eq: np.ndarray | None = None
    b_eq: np.ndarray | None = None
    ub: np.ndarray | None = None
    fixed: dict[int, float] = field(default_factory=dict)
    n: int = 0

    def __post_init__(self) -> None:
        self.c = np.asarray(self.c, dtype=float).ravel()
        self.n = self.c.shape[0]
        if self.sense not in ("min", "max"):
            raise LPInputError(f"sense 必须是 'min' 或 'max'，收到 {self.sense!r}")
        if self.n > MAX_VARIABLES:
            raise LPInputError(
                f"变量数 {self.n} 超过小中规模上限 {MAX_VARIABLES}"
            )
        if self.n == 0:
            raise LPInputError("问题必须至少含一个变量")

        for name, mat, vec in (
            ("A_ub", self.A_ub, self.b_ub),
            ("A_eq", self.A_eq, self.b_eq),
        ):
            if mat is None:
                if vec is not None:
                    raise LPInputError(f"{name} 为 None 但对应右端项非 None")
                continue
            mat_arr = np.asarray(mat, dtype=float)
            vec_arr = np.asarray(vec, dtype=float).ravel()
            if mat_arr.ndim != 2 or mat_arr.shape[1] != self.n:
                raise LPInputError(
                    f"{name} 形状应为 (m, {self.n})，收到 {mat_arr.shape}"
                )
            if mat_arr.shape[0] != vec_arr.shape[0]:
                raise LPInputError(
                    f"{name} 的行数与右端项长度不一致："
                    f"{mat_arr.shape[0]} vs {vec_arr.shape[0]}"
                )
            setattr(self, name, mat_arr)
            setattr(self, "b_ub" if name == "A_ub" else "b_eq", vec_arr)

        n_rows = (self.A_ub.shape[0] if self.A_ub is not None else 0) + (
            self.A_eq.shape[0] if self.A_eq is not None else 0
        )
        if n_rows > MAX_CONSTRAINTS:
            raise LPInputError(
                f"约束行数 {n_rows} 超过小中规模上限 {MAX_CONSTRAINTS}"
            )

        _check_finite_range(self.c, "c")
        for mat in (self.A_ub, self.A_eq):
            if mat is not None:
                _check_finite_range(mat, "约束矩阵")
        for vec_name in ("b_ub", "b_eq"):
            vec = getattr(self, vec_name)
            if vec is not None:
                _check_finite_range(vec, vec_name)

        if self.ub is None:
            self.ub = np.full(self.n, np.inf)
        else:
            self.ub = np.asarray(self.ub, dtype=float).ravel()
            if self.ub.shape[0] > self.n:
                raise LPInputError("ub 长度超过变量数")
            if self.ub.shape[0] < self.n:
                self.ub = np.concatenate(
                    [self.ub, np.full(self.n - self.ub.shape[0], np.inf)]
                )
            if np.any(np.isnan(self.ub)):
                raise LPInputError("ub 中不允许 NaN（无上界请用 null/+Infinity）")
            # 注意：平移后 ub 可能为负（lb > ub 等空框情形），此时交由
            # 求解器正常报告 infeasible，不在构造阶段拒绝。
            finite_ub = self.ub[np.isfinite(self.ub)]
            if finite_ub.size and np.max(np.abs(finite_ub)) > MAX_ABS_COEFF:
                raise LPInputError(f"ub 绝对值超过 {MAX_ABS_COEFF:g}")
        # fixed 由工厂函数 :func:`make_lp` 统一产生
        if not 0 <= len(self.fixed) <= self.n:
            raise LPInputError("fixed 下标数量非法")


def _check_finite_range(a: np.ndarray, what: str) -> None:
    if not np.all(np.isfinite(a)):
        raise LPInputError(f"{what} 中含有 NaN 或无穷大")
    if a.size and np.max(np.abs(a)) > MAX_ABS_COEFF:
        raise LPInputError(
            f"{what} 中系数绝对值超过 {MAX_ABS_COEFF:g}，超出声明的输入范围"
        )


def make_lp(
    c,
    sense: str = "min",
    c0: float = 0.0,
    A_ub=None,
    b_ub=None,
    A_eq=None,
    b_eq=None,
    lb=None,
    ub=None,
) -> LP:
    """构造 :class:`LP`，统一处理 ">="、变量上下界与固定变量。

    ``lb`` / ``ub`` 支持：标量、列表（``None``/``+inf`` 表示无上界）。
    本项目只支持非负变量，因此 ``lb`` 只接受非负数；``lb == ub``
    会被识别为固定变量。
    """
    c_arr = np.asarray(c, dtype=float).ravel()
    n = c_arr.shape[0]

    # --- 上下界 -> 非负 + 显式 ub + fixed ---
    if lb is None:
        lb_arr = np.zeros(n)
    else:
        lb_arr = _bound_vector(lb, n, "lb")
        if np.any(lb_arr < 0):
            raise LPInputError("仅支持非负变量，lb 不能为负")
    ub_in = np.full(n, np.inf) if ub is None else _bound_vector(ub, n, "ub")
    # ub < lb 不在这里拒绝：这是一个合法描述但数值上不可行的问题，
    # 由求解器统一报告 infeasible（平移后产生负上界行 y <= 负数）。

    fixed: dict[int, float] = {}
    eff_ub = np.full(n, np.inf)
    shift = np.zeros(n)
    for j in range(n):
        if lb_arr[j] == ub_in[j]:  # 含两者同为 +inf 的情形下面另行处理
            if not np.isfinite(lb_arr[j]):
                raise LPInputError(f"变量 x{j} 被固定在无穷大")
            fixed[j] = float(lb_arr[j])
            shift[j] = lb_arr[j]
            eff_ub[j] = 0.0  # 平移后 y = x - lb == 0
        elif lb_arr[j] > 0:
            # 平移 x = y + lb，y >= 0；上界平移
            shift[j] = lb_arr[j]
            eff_ub[j] = np.inf if np.isinf(ub_in[j]) else ub_in[j] - lb_arr[j]
        else:
            eff_ub[j] = ub_in[j]

    # c0 保留用户给出的原始常数；变量平移只影响约束右端项与解的回映，
    # 最终目标值统一用原始口径 c^T x + c0 重算（见 solver._optimal_from_y）。
    c0_eff = float(c0)

    def _shift_rhs(A, b):
        if A is None:
            return None, None
        A = np.asarray(A, dtype=float)
        b = np.asarray(b, dtype=float).ravel() - A @ shift
        return A, b

    A_ub_s, b_ub_s = _shift_rhs(A_ub, b_ub)
    A_eq_s, b_eq_s = _shift_rhs(A_eq, b_eq)

    lp = LP(
        c=c_arr.copy(),
        sense=sense,
        c0=c0_eff,
        A_ub=A_ub_s,
        b_ub=b_ub_s,
        A_eq=A_eq_s,
        b_eq=b_eq_s,
        ub=eff_ub,
        fixed=fixed,
    )
    lp.shift = shift  # 供解映射使用
    lp.lb_original = lb_arr
    lp.ub_original = ub_in
    return lp


def _bound_vector(value, n: int, name: str) -> np.ndarray:
    if np.isscalar(value):
        out = np.full(n, float(value))
    else:
        arr = np.asarray(value, dtype=float).ravel()
        if arr.shape[0] != n:
            raise LPInputError(f"{name} 长度应为 {n}，收到 {arr.shape[0]}")
        out = arr.copy()
    if np.any(np.isnan(out)):
        raise LPInputError(f"{name} 中不允许 NaN")
    finite = out[np.isfinite(out)]
    if finite.size and np.max(np.abs(finite)) > MAX_ABS_COEFF:
        raise LPInputError(f"{name} 绝对值超过 {MAX_ABS_COEFF:g}")
    return out


# ---------------------------------------------------------------------------
# 标准化
# ---------------------------------------------------------------------------


@dataclass
class StandardForm:
    r"""标准化结果。

    内部标准形式::

        最小化  cbar^T y
          s.t.  Abar y = bbar,   bbar >= 0
                 y >= 0

    原始变量按 ``y[:n_orig]``（平移后的非负变量）排列，随后依次是
    不等式松弛列、显式上界松弛列、人工列。

    属性
    ------
    Abar, bbar, cbar:
        标准形式的矩阵 / 右端 / 目标系数。
    n_orig:
        原始（平移后）变量列数。
    n_slack_ub:
        一般不等式 ``<=`` 松弛列数。
    n_slack_bound:
        显式上界松弛列数。
    basis0:
        初始基列下标（一般松弛或人工列），每行一个。
    artificial:
        人工列下标数组。
    row_kinds:
        每行 ``("ub"|"eq"|"bound", 原始行号, 基符号)``；
        基符号为 ``+1``（A y <= b 方向）或 ``-1``（原始 ">=" 取反）。
    lp:
        反向指回 :class:`LP`。
    """

    Abar: np.ndarray
    bbar: np.ndarray
    cbar: np.ndarray
    n_orig: int
    n_slack_ub: int
    n_slack_bound: int
    basis0: np.ndarray
    artificial: np.ndarray
    row_kinds: list[tuple[str, int, int]]
    lp: LP


def build_standard_form(lp: LP) -> StandardForm:
    """把 :class:`LP` 化为带人工变量的等式标准形。

    列布局：``结构列 | 一般松弛列 | 上界松弛列 | 人工列``。

    初始基构造（经典两阶段法）：

    1. 每个 ``<=`` 行（含显式上界行）加一个非负松弛列；
       **右端非负**的行直接以松弛变量为初始基。
    2. **等式行**与**右端为负的 ``<=`` 行**加人工变量并以其为初始基；
       后者先把整行取反使 ``bbar >= 0``（等式取反不改变可行集，
       ``<=`` 取反变为 ``>=`` 后由人工变量保证起始可行）。

    因此初始基列构成带符号的单位阵（非负右端行为 +1；取反行的松弛
    列为 -1 但该行用的是人工列，仍为 +1），全部基变量初值即右端项。
    """
    n = lp.n
    rows: list[np.ndarray] = []
    rhs: list[float] = []
    row_kinds: list[tuple[str, int, int]] = []

    if lp.A_ub is not None:
        for i in range(lp.A_ub.shape[0]):
            rows.append(lp.A_ub[i].copy())
            rhs.append(float(lp.b_ub[i]))
            row_kinds.append(("ub", i, 1))
    if lp.A_eq is not None:
        for i in range(lp.A_eq.shape[0]):
            rows.append(lp.A_eq[i].copy())
            rhs.append(float(lp.b_eq[i]))
            row_kinds.append(("eq", i, 1))

    bound_rows: list[int] = []
    for j in range(n):
        if np.isfinite(lp.ub[j]):
            v = np.zeros(n)
            v[j] = 1.0
            rows.append(v)
            rhs.append(float(lp.ub[j]))
            bound_rows.append(len(row_kinds))
            row_kinds.append(("bound", j, 1))

    m = len(rows)
    n_slack_ub = lp.A_ub.shape[0] if lp.A_ub is not None else 0
    n_slack_bound = len(bound_rows)
    n_slack = n_slack_ub + n_slack_bound

    n_cols = n + n_slack
    slack_row_for: dict[int, int] = {}
    for idx, r in enumerate(range(n_slack_ub)):
        slack_row_for[r] = n + idx
    for k, r in enumerate(bound_rows):
        slack_row_for[r] = n + n_slack_ub + k

    base_sign = np.ones(m, dtype=int)
    # 先确定哪些行需要人工列：等式行总是需要；<= 行仅当右端为负。
    eq_rows = {
        r for r, (kind, _, _) in enumerate(row_kinds) if kind == "eq"
    }
    art_rows: list[int] = []
    for r in range(m):
        is_eq = r in eq_rows
        if is_eq:
            art_rows.append(r)
            if rhs[r] < -TOL.feas:
                rows[r] = -rows[r]
                rhs[r] = -rhs[r]
                base_sign[r] = -1
        elif rhs[r] < -TOL.feas:
            # 负右端 <= 行：取反（变成 >=），加人工列。
            rows[r] = -rows[r]
            rhs[r] = -rhs[r]
            base_sign[r] = -1
            art_rows.append(r)

    n_art = len(art_rows)
    total_cols = n_cols + n_art
    A = np.zeros((m, total_cols))
    b = np.array(rhs, dtype=float)
    for r, vec in enumerate(rows):
        A[r, :n] = vec

    art_col_for = {r: n_cols + k for k, r in enumerate(art_rows)}
    basis = np.empty(m, dtype=int)
    for r in range(m):
        if r in art_col_for:
            basis[r] = art_col_for[r]
            A[r, basis[r]] = 1.0
        else:
            basis[r] = slack_row_for[r]
            # 未取反的 <= 行松弛列为 +1；负右端行已翻转并改用人工列，
            # 不会进入此分支（其松弛列仍需写 -1 以保持等式语义）。
            A[r, basis[r]] = 1.0
    # 被取反行的松弛列系数为 -1（人工列单独为 +1）。
    for r in art_rows:
        if r in slack_row_for:
            A[r, slack_row_for[r]] = -1.0

    cbar = np.zeros(total_cols)
    cbar[:n] = lp.c if lp.sense == "min" else -lp.c

    artificial = np.array([art_col_for[r] for r in art_rows], dtype=int)
    final_kinds = [
        (kind, idx, int(base_sign[r]))
        for r, (kind, idx, _) in enumerate(row_kinds)
    ]

    if total_cols > MAX_COLUMNS:
        raise LPInputError(
            f"标准化后列数 {total_cols} 超过上限 {MAX_COLUMNS}"
        )

    return StandardForm(
        Abar=A,
        bbar=b,
        cbar=cbar,
        n_orig=n,
        n_slack_ub=n_slack_ub,
        n_slack_bound=n_slack_bound,
        basis0=basis,
        artificial=artificial,
        row_kinds=final_kinds,
        lp=lp,
    )
