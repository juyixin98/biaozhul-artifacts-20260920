"""CSR（Compressed Sparse Row）稀疏矩阵。

存储约定（与 scipy.sparse.csr_matrix 的三元组一致）：

* ``data``    长度 nnz，非零元素的数值（仅支持 float64）；
* ``indices`` 长度 nnz，每个非零元素的列下标；每行内必须严格递增、不重复；
* ``indptr``  长度 n+1，第 i 行的元素切片为 data[indptr[i]:indptr[i+1]]。

本模块只负责：结构校验、矩阵-向量乘法、对角元提取、稠密矩阵构造、
以及对称性检查。是否正定由 PCG 在迭代中通过曲率判据实际检验。
"""

from __future__ import annotations

import numpy as np

from .errors import RequestError

# 结构性限制：CSR 每一行允许的非零个数上限，防止单个请求占用过多资源。
MAX_NNZ = 1_000_000
MAX_NNZ_PER_ROW = 10_000


class CSRMatrix:
    """校验过的 CSR 稀疏方阵。

    构造时执行全部结构检查（越界、行指针单调性、列下标排序/重复、
    数值有限性）。构造成功即保证：

    * ``0 <= n <= MAX_NNZ`` 维方阵；
    * 行内列下标严格递增、互不重复、全部在 [0, n) 内；
    * 所有数值有限（无 NaN/Inf）。

    注意：构造成功**不**保证对称或正定，那两项由
    :func:`check_symmetric` 与 PCG 运行时曲率检查负责。
    """

    __slots__ = ("n", "data", "indices", "indptr")

    def __init__(
        self,
        data: np.ndarray,
        indices: np.ndarray,
        indptr: np.ndarray,
        n: int | None = None,
    ) -> None:
        data = np.asarray(data)
        indices = np.asarray(indices)
        indptr = np.asarray(indptr)

        # 空整数数组常被 numpy 推断为 float64（如 np.array([])）：
        # 在检查 dtype 之前先按“空且全整数位型”宽容处理。
        if indices.size == 0 and indices.dtype.kind == "f":
            indices = indices.astype(np.intp)
        if indptr.size == 0 and indptr.dtype.kind == "f":
            indptr = indptr.astype(np.intp)

        # ---- 形状 / dtype 检查 -------------------------------------------------
        if data.ndim != 1:
            raise RequestError("data_not_vector", "data 必须是一维数组")
        if indices.ndim != 1:
            raise RequestError("indices_not_vector", "indices 必须是一维数组")
        if indptr.ndim != 1:
            raise RequestError("indptr_not_vector", "indptr 必须是一维数组")
        if data.dtype.kind != "f":
            raise RequestError(
                "data_not_numeric", f"data 必须是浮点数组，实际 dtype={data.dtype}"
            )
        if indices.dtype.kind not in ("i", "u"):
            raise RequestError(
                "indices_not_integer",
                f"indices 必须是整数数组，实际 dtype={indices.dtype}",
            )
        if indptr.dtype.kind not in ("i", "u"):
            raise RequestError(
                "indptr_not_integer",
                f"indptr 必须是整数数组，实际 dtype={indptr.dtype}",
            )

        nnz = data.shape[0]
        if indices.shape[0] != nnz:
            raise RequestError(
                "indices_length_mismatch",
                f"indices 长度({indices.shape[0]})必须与 data 长度({nnz})一致",
            )
        if n is None:
            n = int(indptr.shape[0]) - 1
        else:
            if not isinstance(n, int) or isinstance(n, bool):
                raise RequestError("n_not_integer", "n 必须是整数")
            if int(indptr.shape[0]) != n + 1:
                raise RequestError(
                    "indptr_length_mismatch",
                    f"n={n} 时 indptr 长度必须为 {n + 1}，实际为 {indptr.shape[0]}",
                )
        if n < 0:
            raise RequestError("n_negative", f"n 必须非负，实际 n={n}")
        if n == 0:
            # 零维矩阵单独处理：data/indices 必须为空，indptr == [0]
            if nnz != 0:
                raise RequestError("data_nonempty_for_zero_dim", "n=0 时 data 必须为空")
            if int(indptr[0]) != 0:
                raise RequestError("indptr_invalid", "indptr[0] 必须为 0")
            self.n = 0
            self.data = np.empty(0, dtype=np.float64)
            self.indices = np.empty(0, dtype=np.intp)
            self.indptr = np.array([0], dtype=np.intp)
            return

        if nnz > MAX_NNZ:
            raise RequestError(
                "too_many_nonzeros",
                f"非零元素数 {nnz} 超过上限 {MAX_NNZ}（本库限定小、中规模问题）",
            )

        # ---- 数值有限性 --------------------------------------------------------
        data64 = data.astype(np.float64, copy=False)
        if not np.all(np.isfinite(data64)):
            raise RequestError(
                "data_not_finite", "data 中存在 NaN 或 Inf，所有数值必须有限"
            )

        # ---- indptr 检查：起点为 0、单调不减、终点为 nnz ------------------------
        indptr_i = indptr.astype(np.intp, copy=False)
        if int(indptr_i[0]) != 0:
            raise RequestError("indptr_invalid", "indptr[0] 必须为 0")
        if np.any(np.diff(indptr_i) < 0):
            raise RequestError(
                "indptr_not_monotonic", "indptr 必须单调不减（行间切片不得重叠倒退）"
            )
        if int(indptr_i[-1]) != nnz:
            raise RequestError(
                "indptr_invalid", f"indptr[-1]={int(indptr_i[-1])} 必须等于 nnz={nnz}"
            )
        row_widths = np.diff(indptr_i)
        if np.any(row_widths > MAX_NNZ_PER_ROW):
            bad = int(np.argmax(row_widths > MAX_NNZ_PER_ROW))
            raise RequestError(
                "too_many_nonzeros_per_row",
                f"第 {bad} 行非零个数 {int(row_widths[bad])} 超过每行上限 "
                f"{MAX_NNZ_PER_ROW}",
            )

        # ---- indices 检查：越界、行内严格递增（含重复检测）----------------------
        idx_i = indices.astype(np.intp, copy=False)
        if np.any((idx_i < 0) | (idx_i >= n)):
            bad = int(np.argmax((idx_i < 0) | (idx_i >= n)))
            raise RequestError(
                "index_out_of_bounds",
                f"indices[{bad}]={int(idx_i[bad])} 越界，合法列下标范围为 [0, {n - 1}]",
            )
        for i in range(n):
            lo, hi = int(indptr_i[i]), int(indptr_i[i + 1])
            if hi - lo >= 2 and np.any(np.diff(idx_i[lo:hi]) <= 0):
                raise RequestError(
                    "indices_not_sorted",
                    f"第 {i} 行的列下标未严格递增（存在乱序或重复列）；"
                    "本库要求先合并重复列并排序",
                )

        self.n = n
        self.data = data64
        self.indices = idx_i
        self.indptr = indptr_i

    # ------------------------------------------------------------------ #
    # 基础运算
    # ------------------------------------------------------------------ #
    def matvec(self, x: np.ndarray) -> np.ndarray:
        """计算 y = A @ x（仅校验长度，不拷贝写入）。"""
        x = np.asarray(x, dtype=np.float64)
        if x.shape != (self.n,):
            raise RequestError(
                "dimension_mismatch",
                f"向量长度 {x.shape[0]} 与矩阵阶数 {self.n} 不一致",
            )
        y = np.zeros(self.n, dtype=np.float64)
        # 逐行做点积；对稀疏行，np.dot 走分段 BLAS，效率足够中小规模使用。
        for i in range(self.n):
            lo, hi = int(self.indptr[i]), int(self.indptr[i + 1])
            if hi > lo:
                y[i] = np.dot(self.data[lo:hi], x[self.indices[lo:hi]])
        return y

    def diagonal(self) -> np.ndarray:
        """返回对角元素向量；缺少对角元的位置记为 0.0。"""
        diag = np.zeros(self.n, dtype=np.float64)
        for i in range(self.n):
            lo, hi = int(self.indptr[i]), int(self.indptr[i + 1])
            seg = self.indices[lo:hi]
            p = np.searchsorted(seg, i)
            if p < hi - lo and int(seg[p]) == i:
                diag[i] = float(self.data[lo + p])
        return diag

    # ------------------------------------------------------------------ #
    # 辅助构造
    # ------------------------------------------------------------------ #
    @classmethod
    def from_dense(cls, a: np.ndarray) -> "CSRMatrix":
        """从二维稠密数组构造 CSR（测试/样例生成用），零元素不存储。"""
        a = np.asarray(a, dtype=np.float64)
        if a.ndim != 2 or a.shape[0] != a.shape[1]:
            raise RequestError("not_square_matrix", "from_dense 需要二维方阵")
        n = a.shape[0]
        data, indices, indptr = [], [], [0]
        for i in range(n):
            row = a[i]
            (nz,) = np.nonzero(row)
            indices.extend(int(j) for j in nz)
            data.extend(float(row[j]) for j in nz)
            indptr.append(len(data))
        return cls(
            np.asarray(data, dtype=np.float64),
            np.asarray(indices, dtype=np.intp),
            np.asarray(indptr, dtype=np.intp),
            n,
        )

    def to_dense(self) -> np.ndarray:
        """还原为稠密矩阵（主要用于测试与诊断）。"""
        a = np.zeros((self.n, self.n), dtype=np.float64)
        for i in range(self.n):
            lo, hi = int(self.indptr[i]), int(self.indptr[i + 1])
            a[i, self.indices[lo:hi]] = self.data[lo:hi]
        return a

    def nnz(self) -> int:
        return int(self.data.shape[0])


def check_symmetric(a: CSRMatrix, tol: float = 1e-9) -> None:
    """检查稀疏矩阵是否（数值上）对称，不对称则抛出 ``matrix_not_symmetric``。

    判据：对每个上三角元素 A[i,j]（i<j），在第 j 行查找列 i：

    * 结构缺失（(j,i) 位置没有存储元素）即判定不对称；
    * 数值上要求 |A[i,j] - A[j,i]| <= tol * max(1, |A[i,j]|, |A[j,i]|)。

    复杂度 O(nnz·log(行宽))，无需构造稠密矩阵。
    """
    n = a.n
    for i in range(n):
        lo, hi = int(a.indptr[i]), int(a.indptr[i + 1])
        for k in range(lo, hi):
            j = int(a.indices[k])
            if j <= i:
                continue
            l2, h2 = int(a.indptr[j]), int(a.indptr[j + 1])
            seg = a.indices[l2:h2]
            p = int(np.searchsorted(seg, i))
            if p >= h2 - l2 or int(seg[p]) != i:
                raise RequestError(
                    "matrix_not_symmetric",
                    f"矩阵非对称：A[{i},{j}]={float(a.data[k]):g} 存在，"
                    f"但 A[{j},{i}] 缺失",
                )
            transposed = float(a.data[l2 + p])
            v = float(a.data[k])
            scale = max(1.0, abs(v), abs(transposed))
            if abs(v - transposed) > tol * scale:
                raise RequestError(
                    "matrix_not_symmetric",
                    f"矩阵非对称：|A[{i},{j}] - A[{j},{i}]| = {abs(v - transposed):.3e}"
                    f" 超过容差 {tol:.0e}（相对量级 {scale:.3g}）",
                )
