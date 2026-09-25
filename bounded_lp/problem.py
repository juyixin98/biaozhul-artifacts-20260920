"""线性规划问题的数据结构与标准形预处理。

用户问题::

    min/max  c^T x
    s.t. A_ub x <= b_ub
         A_eq x  = b_eq
         0 <= lb <= x <= ub          （lb 默认 0，ub 默认 +∞）

被转换为标准形::

    min  c_std^T x_std + obj_const
    s.t. A_std x_std = b_std,  b_std >= 0
         x_std >= 0

转换规则：
1. maximize 转为 minimize（费用取负，最终目标值再取负）。
2. 有限下界做平移 ``x = lb + z, z >= 0``，产生常数项 ``c^T lb``。
3. 有限上界 ``x <= ub`` 转为附加的 ``<=`` 行 ``z <= ub - lb``。
4. ``<=`` 且右端非负：加 **松弛变量**（系数 +1），该列天然是初始基列。
5. ``<=`` 但右端为负：整行乘 -1 变成 ``>=``，加 **剩余变量**(-1) 与 **人工变量**(+1)。
6. ``=`` 等式：右端取负号规范化为非负，加 **人工变量**(+1)。
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from bounded_lp.errors import InvalidProblem
from bounded_lp.tolerance import MAX_ABS_VALUE, MAX_CONSTRAINTS, MAX_VARIABLES


@dataclass(frozen=True)
class StandardForm:
    """预处理后的标准形。

    Attributes:
        A: 标准形约束矩阵，形状 (m, N)。
        b: 右端项，形状 (m,)，满足 b >= 0。
        c: 标准形费用（已含 max→min 取负），形状 (N,)。
        obj_const: 目标常数项（max 取负 + 下界平移产生）。
        n_orig: 前 ``n_orig`` 列对应平移后的原变量 z。
        lb: 原始变量下界（用于由 z 还原 x）。
        sense: 原始优化方向，"min" 或 "max"。
        n_slack: 松弛变量数；其后依次是剩余、人工变量列。
        n_surplus: 剩余变量数。
        n_artificial: 人工变量数。
        artificial_basis_cols: 初始基所在的列索引（松弛列 + 人工列）。
    """

    A: np.ndarray
    b: np.ndarray
    c: np.ndarray
    obj_const: float
    n_orig: int
    lb: np.ndarray
    sense: str
    n_slack: int
    n_surplus: int
    n_artificial: int
    artificial_basis_cols: tuple[int, ...]

    def to_original_x(self, x_std: np.ndarray) -> np.ndarray:
        """把标准形解还原为原始变量 x = lb + z。"""
        return self.lb + np.asarray(x_std, dtype=float)[: self.n_orig]

    def original_objective(self, std_value: float) -> float:
        """标准形最小化值 → 原始问题目标值。"""
        total = std_value + self.obj_const
        return -total if self.sense == "max" else total


@dataclass
class LPProblem:
    """线性规划问题（面向用户的形式）。"""

    c: np.ndarray
    sense: str = "min"
    A_ub: np.ndarray | None = None
    b_ub: np.ndarray | None = None
    A_eq: np.ndarray | None = None
    b_eq: np.ndarray | None = None
    lb: np.ndarray | None = None
    ub: np.ndarray | None = None

    # -- 构造与校验 --------------------------------------------------------

    def __post_init__(self) -> None:
        self.c = np.atleast_1d(np.asarray(self.c, dtype=float))
        if self.c.ndim != 1:
            raise InvalidProblem("c 必须是一维向量")
        self.n = int(self.c.size)
        if self.n == 0:
            raise InvalidProblem("至少需要 1 个变量")
        if self.n > MAX_VARIABLES:
            raise InvalidProblem(f"变量数 {self.n} 超过上限 {MAX_VARIABLES}")
        if self.sense not in ("min", "max"):
            raise InvalidProblem(f"sense 只能是 'min' 或 'max'，收到 {self.sense!r}")

        self.A_ub = self._check_matrix(self.A_ub, "A_ub")
        self.b_ub = self._check_vector(self.b_ub, "b_ub", self.A_ub.shape[0])
        self.A_eq = self._check_matrix(self.A_eq, "A_eq")
        self.b_eq = self._check_vector(self.b_eq, "b_eq", self.A_eq.shape[0])

        if self.lb is None:
            self.lb = np.zeros(self.n)
        else:
            self.lb = self._check_bounds(np.asarray(self.lb, dtype=float), "lb")
        if self.ub is None:
            self.ub = np.full(self.n, np.inf)
        else:
            self.ub = self._check_bounds(np.asarray(self.ub, dtype=float), "ub", allow_inf=True)

        if np.any(self.ub < self.lb):
            raise InvalidProblem("存在 ub < lb 的变量")
        if np.any(self.lb < 0.0):
            raise InvalidProblem("本求解器仅支持非负变量：lb 的每个分量必须 >= 0")

        m_total = self.A_ub.shape[0] + self.A_eq.shape[0] + int(np.sum(np.isfinite(self.ub)))
        if m_total > MAX_CONSTRAINTS:
            raise InvalidProblem(f"约束总数 {m_total} 超过上限 {MAX_CONSTRAINTS}")

        self._check_magnitude()

    def _check_matrix(self, value, name: str) -> np.ndarray:
        if value is None:
            return np.zeros((0, self.n))
        arr = np.atleast_2d(np.asarray(value, dtype=float))
        if arr.ndim != 2 or arr.shape[1] != self.n:
            raise InvalidProblem(f"{name} 形状必须为 (m, {self.n})，收到 {arr.shape}")
        return arr

    def _check_vector(self, value, name: str, m: int) -> np.ndarray:
        if value is None:
            if m != 0:
                raise InvalidProblem(f"提供了约束矩阵但缺少 {name}")
            return np.zeros(0)
        arr = np.atleast_1d(np.asarray(value, dtype=float))
        if arr.shape != (m,):
            raise InvalidProblem(f"{name} 长度必须为 {m}，收到 {arr.shape}")
        return arr

    def _check_bounds(self, arr: np.ndarray, name: str, allow_inf: bool = False) -> np.ndarray:
        if arr.shape != (self.n,):
            raise InvalidProblem(f"{name} 长度必须为 {self.n}，收到 {arr.shape}")
        if np.any(np.isnan(arr)):
            raise InvalidProblem(f"{name} 不允许 NaN")
        if not allow_inf and np.any(np.isinf(arr)):
            raise InvalidProblem(f"{name} 必须有限（仅 ub 允许 +∞）")
        if allow_inf and np.any(arr == -np.inf):
            raise InvalidProblem(f"{name} 不允许 -∞")
        return arr

    def _check_magnitude(self) -> None:
        """对有限数值做范围检查；NaN 一律拒绝。"""

        def bad(arr: np.ndarray) -> bool:
            finite = arr[np.isfinite(arr)]
            return bool(np.any(np.isnan(arr)) or np.any(np.abs(finite) > MAX_ABS_VALUE))

        if bad(self.c) or bad(self.A_ub) or bad(self.b_ub) or bad(self.A_eq) or bad(self.b_eq):
            raise InvalidProblem(f"存在 NaN 或绝对值超过 {MAX_ABS_VALUE:g} 的系数/右端项")
        for arr in (self.lb, self.ub):
            finite = arr[np.isfinite(arr)]
            if np.any(np.abs(finite) > MAX_ABS_VALUE):
                raise InvalidProblem(f"边界绝对值超过 {MAX_ABS_VALUE:g}")

    # -- 标准形转换 --------------------------------------------------------

    def standard_form(self) -> StandardForm:
        """构造标准形（见模块文档字符串）。"""
        n = self.n
        sign = -1.0 if self.sense == "max" else 1.0
        f = sign * self.c                       # 最小化 min f^T x
        obj_const = float(f @ self.lb)          # x = lb + z 平移常数

        # 收集“行 + 行类型”：ineq 表示 <=（在平移后的坐标系）
        rows: list[np.ndarray] = []
        rhs: list[float] = []
        kinds: list[str] = []                   # "le" 或 "eq"

        for i in range(self.A_ub.shape[0]):
            rows.append(self.A_ub[i].copy())
            rhs.append(float(self.b_ub[i] - self.A_ub[i] @ self.lb))
            kinds.append("le")
        for j in np.nonzero(np.isfinite(self.ub))[0]:
            row = np.zeros(n)
            row[int(j)] = 1.0
            rows.append(row)
            rhs.append(float(self.ub[int(j)] - self.lb[int(j)]))
            kinds.append("le")
        for i in range(self.A_eq.shape[0]):
            rows.append(self.A_eq[i].copy())
            rhs.append(float(self.b_eq[i] - self.A_eq[i] @ self.lb))
            kinds.append("eq")

        m = len(rows)
        if m == 0:
            # 无约束：标准形为 0 行，列仅含 z
            return StandardForm(
                A=np.zeros((0, n)),
                b=np.zeros(0),
                c=f.copy(),
                obj_const=obj_const,
                n_orig=n,
                lb=self.lb.copy(),
                sense=self.sense,
                n_slack=0,
                n_surplus=0,
                n_artificial=0,
                artificial_basis_cols=(),
            )

        # 第一遍：统计松弛/剩余/人工列数
        n_slack = sum(kinds[i] == "le" for i in range(m))
        n_surplus = sum(kinds[i] == "le" and rhs[i] < 0.0 for i in range(m))
        n_artificial = n_surplus + sum(k == "eq" for k in kinds)

        N = n + n_slack + n_surplus + n_artificial
        A = np.zeros((m, N))
        b = np.zeros(m)
        basis_cols: list[int] = []

        i_slack = n
        i_surplus = n + n_slack
        i_art = n + n_slack + n_surplus

        for r in range(m):
            row = np.array(rows[r], dtype=float)
            beta = rhs[r]
            if kinds[r] == "le" and beta >= 0.0:
                A[r, :n] = row
                A[r, i_slack] = 1.0
                b[r] = beta
                basis_cols.append(i_slack)
                i_slack += 1
            elif kinds[r] == "le":
                # beta < 0：乘 -1，<= 变 >=，剩余变量 + 人工变量
                A[r, :n] = -row
                A[r, i_surplus] = -1.0
                A[r, i_art] = 1.0
                b[r] = -beta
                basis_cols.append(i_art)
                i_surplus += 1
                i_art += 1
            else:  # eq
                if beta < 0.0:
                    A[r, :n] = -row
                    b[r] = -beta
                else:
                    A[r, :n] = row
                    b[r] = beta
                A[r, i_art] = 1.0
                basis_cols.append(i_art)
                i_art += 1

        c_std = np.zeros(N)
        c_std[:n] = f

        return StandardForm(
            A=A,
            b=b,
            c=c_std,
            obj_const=obj_const,
            n_orig=n,
            lb=self.lb.copy(),
            sense=self.sense,
            n_slack=n_slack,
            n_surplus=n_surplus,
            n_artificial=n_artificial,
            artificial_basis_cols=tuple(basis_cols),
        )

    # -- 便捷工厂 ---------------------------------------------------------

    @staticmethod
    def from_dict(data: dict) -> "LPProblem":
        """从 JSON 兼容字典构造（字段校验见 io_json，这里只做形状校验）。"""
        if "c" not in data:
            raise InvalidProblem("缺少字段 c（目标系数）")
        return LPProblem(
            c=data["c"],
            sense=str(data.get("sense", "min")),
            A_ub=data.get("A_ub"),
            b_ub=data.get("b_ub"),
            A_eq=data.get("A_eq"),
            b_eq=data.get("b_eq"),
            lb=data.get("lb"),
            ub=data.get("ub"),
        )
