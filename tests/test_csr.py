"""CSR 矩阵：结构校验、矩阵-向量乘法、对称性检查。"""

from __future__ import annotations

import unittest

import numpy as np

from sparse_cg import CSRMatrix, RequestError, check_symmetric


def tri_csr(n: int) -> CSRMatrix:
    """对称三对角矩阵：对角 2，次对角 -1（一维拉普拉斯）。"""
    data, indices, indptr = [], [], [0]
    for i in range(n):
        if i > 0:
            data.append(-1.0)
            indices.append(i - 1)
        data.append(2.0)
        indices.append(i)
        if i < n - 1:
            data.append(-1.0)
            indices.append(i + 1)
        indptr.append(len(data))
    return CSRMatrix(np.array(data), np.array(indices), np.array(indptr), n)


class TestCSRValid(unittest.TestCase):
    def test_matvec_matches_dense(self) -> None:
        rng = np.random.default_rng(0)
        d = rng.standard_normal((6, 6))
        dense = d + d.T + 12.0 * np.eye(6)  # 对角占优，保证正对角
        a = CSRMatrix.from_dense(dense)
        x = rng.standard_normal(6)
        np.testing.assert_allclose(a.matvec(x), dense @ x, atol=1e-14)

    def test_laplacian_matvec(self) -> None:
        a = tri_csr(4)
        np.testing.assert_allclose(
            a.matvec(np.ones(4)),
            np.array([1.0, 0.0, 0.0, 1.0]),
        )

    def test_diagonal(self) -> None:
        a = tri_csr(5)
        np.testing.assert_array_equal(a.diagonal(), np.full(5, 2.0))

    def test_nnz_and_roundtrip(self) -> None:
        a = tri_csr(5)
        self.assertEqual(a.nnz(), 13)
        dense = a.to_dense()
        self.assertEqual(dense.shape, (5, 5))
        np.testing.assert_array_equal(dense[0, 1], -1.0)
        np.testing.assert_array_equal(dense[1, 0], -1.0)

    def test_zero_dim(self) -> None:
        a = CSRMatrix(np.array([]), np.array([]), np.array([0]), 0)
        self.assertEqual(a.n, 0)
        np.testing.assert_array_equal(a.matvec(np.array([])), np.array([]))


class TestCSRInvalid(unittest.TestCase):
    def _check(self, code: str, **kwargs) -> None:
        with self.assertRaises(RequestError) as ctx:
            CSRMatrix(**kwargs)
        self.assertEqual(ctx.exception.code, code)

    def test_index_out_of_bounds_high(self) -> None:
        self._check(
            "index_out_of_bounds",
            data=np.array([1.0, 2.0]),
            indices=np.array([0, 2]),
            indptr=np.array([0, 1, 2]),
            n=2,
        )

    def test_index_out_of_bounds_negative(self) -> None:
        self._check(
            "index_out_of_bounds",
            data=np.array([1.0, 2.0]),
            indices=np.array([-1, 0]),
            indptr=np.array([0, 1, 2]),
            n=2,
        )

    def test_indptr_not_start_zero(self) -> None:
        self._check(
            "indptr_invalid",
            data=np.array([1.0]),
            indices=np.array([0]),
            indptr=np.array([1, 1]),
            n=1,
        )

    def test_indptr_end_not_nnz(self) -> None:
        self._check(
            "indptr_invalid",
            data=np.array([1.0, 2.0]),
            indices=np.array([0, 1]),
            indptr=np.array([0, 1]),
            n=1,
        )

    def test_indptr_not_monotonic(self) -> None:
        # n=3，end==nnz 成立，但 indptr 内部从 2 退到 1
        self._check(
            "indptr_not_monotonic",
            data=np.array([1.0, 2.0, 3.0]),
            indices=np.array([0, 1, 2]),
            indptr=np.array([0, 2, 1, 3]),
            n=3,
        )

    def test_duplicate_column(self) -> None:
        self._check(
            "indices_not_sorted",
            data=np.array([1.0, 2.0]),
            indices=np.array([0, 0]),
            indptr=np.array([0, 2, 2]),
            n=2,
        )

    def test_unsorted_column(self) -> None:
        self._check(
            "indices_not_sorted",
            data=np.array([1.0, 2.0, 3.0]),
            indices=np.array([1, 0, 0]),
            indptr=np.array([0, 2, 3]),
            n=2,
        )

    def test_data_length_mismatch(self) -> None:
        self._check(
            "indices_length_mismatch",
            data=np.array([1.0]),
            indices=np.array([0, 1]),
            indptr=np.array([0, 2]),
            n=2,
        )

    def test_indptr_length_mismatch(self) -> None:
        self._check(
            "indptr_length_mismatch",
            data=np.array([1.0]),
            indices=np.array([0]),
            indptr=np.array([0, 1, 1]),
            n=1,
        )

    def test_float_indices_rejected(self) -> None:
        self._check(
            "indices_not_integer",
            data=np.array([1.0]),
            indices=np.array([0.0]),
            indptr=np.array([0, 1]),
            n=1,
        )

    def test_nan_data_rejected(self) -> None:
        self._check(
            "data_not_finite",
            data=np.array([np.nan]),
            indices=np.array([0]),
            indptr=np.array([0, 1]),
            n=1,
        )

    def test_inf_data_rejected(self) -> None:
        self._check(
            "data_not_finite",
            data=np.array([np.inf]),
            indices=np.array([0]),
            indptr=np.array([0, 1]),
            n=1,
        )

    def test_too_many_nonzeros(self) -> None:
        # nnz 检查先于 indptr 内容检查，此处只需 data/indices 等长
        n = 5
        self._check(
            "too_many_nonzeros",
            data=np.zeros(1_000_001),
            indices=np.zeros(1_000_001, dtype=np.int64),
            indptr=np.zeros(n + 1, dtype=np.int64),
            n=n,
        )


class TestSymmetry(unittest.TestCase):
    def test_symmetric_ok(self) -> None:
        check_symmetric(tri_csr(6))  # 不抛异常即通过

    def test_structural_asymmetry(self) -> None:
        # [[1, 2, 0], [0, 1, 0], [0, 0, 1]]：(0,1) 存在而 (1,0) 缺失
        a = CSRMatrix(
            data=np.array([1.0, 2.0, 1.0, 1.0]),
            indices=np.array([0, 1, 1, 2]),
            indptr=np.array([0, 2, 3, 4]),
            n=3,
        )
        with self.assertRaises(RequestError) as ctx:
            check_symmetric(a)
        self.assertEqual(ctx.exception.code, "matrix_not_symmetric")

    def test_numeric_asymmetry(self) -> None:
        # 结构对称但 A[0,1]=2, A[1,0]=1
        a = CSRMatrix(
            data=np.array([1.0, 2.0, 1.0, 1.0, 1.0]),
            indices=np.array([0, 1, 0, 1, 2]),
            indptr=np.array([0, 2, 4, 5]),
            n=3,
        )
        with self.assertRaises(RequestError) as ctx:
            check_symmetric(a)
        self.assertEqual(ctx.exception.code, "matrix_not_symmetric")

    def test_tolerance_scaled(self) -> None:
        # 1e-10 量级的不对称在 1e-9 相对容差内（且数值量级为 1）
        a = CSRMatrix(
            data=np.array([1.0, 1.0 + 5e-10, 1.0, 1.0]),
            indices=np.array([0, 1, 0, 1]),
            indptr=np.array([0, 2, 4]),
            n=2,
        )
        check_symmetric(a, tol=1e-9)  # 不抛异常


if __name__ == "__main__":
    unittest.main()
