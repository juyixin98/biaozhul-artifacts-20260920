"""Tests for CSR construction, validation, canonicalization and matvec."""

from __future__ import annotations

import numpy as np
import unittest

from sparse_cg import CSRMatrix, InvalidCsrError
from tests._utils import dense_to_csr


class TestCSRConstruction(unittest.TestCase):

    def test_basic_diagonal(self):
        A = CSRMatrix(3, [0, 1, 2, 3], [0, 1, 2], [2.0, 3.0, 4.0])
        self.assertEqual((A.n, A.nnz), (3, 3))
        np.testing.assert_allclose(A.diagonal(), [2.0, 3.0, 4.0])
        np.testing.assert_allclose(A.matvec(np.ones(3)), [2.0, 3.0, 4.0])

    def test_from_triplets_and_matvec_against_dense(self):
        rng = np.random.default_rng(0)
        n = 12
        D = rng.standard_normal((n, n))
        D = D + D.T + n * np.eye(n)  # symmetric, strictly dominant
        A = dense_to_csr(D)
        x = rng.standard_normal(n)
        np.testing.assert_allclose(A.matvec(x), D @ x, rtol=1e-12, atol=1e-12)

    def test_duplicate_entries_are_summed_in_order(self):
        # Two representations of the same matrix:
        # clean:      (0,0)=2 (0,1)=-1 (1,1)=2
        # duplicated: (0,0)=1.25 +0.75, (0,1)=-0.4-0.6
        clean = CSRMatrix(2, [0, 2, 3], [0, 1, 1], [2.0, -1.0, 2.0])
        dup = CSRMatrix(
            2, [0, 4, 5], [0, 0, 1, 1, 1],
            [1.25, 0.75, -0.4, -0.6, 2.0],
        )
        np.testing.assert_allclose(dup.data, clean.data)
        np.testing.assert_array_equal(dup.indices, clean.indices)
        np.testing.assert_array_equal(dup.indptr, clean.indptr)
        self.assertEqual(dup.nnz, clean.nnz)

    def test_unsorted_per_row_is_sorted(self):
        A = CSRMatrix(2, [0, 2, 4], [1, 0, 1, 0], [-1.0, 2.0, 2.0, -1.0])
        np.testing.assert_array_equal(A.indices[:2], [0, 1])
        np.testing.assert_array_equal(A.indices[2:], [0, 1])

    def test_explicit_zero_after_sum_is_dropped(self):
        # (0,1): 3 + (-3) = 0 -> dropped
        A = CSRMatrix(2, [0, 3, 4], [0, 1, 1, 1], [2.0, 3.0, -3.0, 2.0])
        self.assertEqual(A.nnz, 2)
        np.testing.assert_array_equal(A.indices, [0, 1])

    def test_triplet_duplicates(self):
        A = CSRMatrix.from_triplets(
            2, [0, 0, 1], [0, 0, 1], [1.5, 0.5, -2.0]
        )
        self.assertEqual(A.nnz, 2)
        self.assertAlmostEqual(float(A.data[0]), 2.0)
        self.assertAlmostEqual(float(A.data[1]), -2.0)

    def test_repr(self):
        A = CSRMatrix(2, [0, 1, 2], [0, 1], [1.0, 2.0])
        self.assertIn("n=2", repr(A))
        self.assertIn("nnz=2", repr(A))


class TestCSRValidation(unittest.TestCase):
    """Every illegal CSR variant must raise InvalidCsrError with a message."""

    def _expect(self, n, indptr, indices, data):
        with self.assertRaises(InvalidCsrError):
            CSRMatrix(n, indptr, indices, data)

    def test_n_not_positive(self):
        self._expect(0, [0], [], [])

    def test_n_wrong_type_and_bool_rejected(self):
        self._expect(True, [0, 1], [0], [1.0])  # bool is not an int
        self._expect(2.5, [0, 1, 2], [0, 1], [1.0, 2.0])

    def test_indptr_wrong_length(self):
        self._expect(3, [0, 1, 2], [0, 1], [1.0, 2.0])

    def test_indptr_first_not_zero(self):
        self._expect(2, [1, 2, 3], [0, 1], [1.0, 2.0])

    def test_indptr_last_not_nnz(self):
        self._expect(2, [0, 1, 3], [0, 1], [1.0, 2.0])

    def test_indptr_not_monotonic(self):
        self._expect(
            3, [0, 2, 1, 3], [0, 1, 0], [1.0, 2.0, 3.0]
        )

    def test_negative_column_index(self):
        self._expect(2, [0, 1, 2], [-1, 1], [1.0, 2.0])

    def test_column_index_out_of_range(self):
        self._expect(2, [0, 1, 2], [0, 2], [1.0, 2.0])

    def test_indices_data_length_mismatch(self):
        with self.assertRaises(InvalidCsrError):
            CSRMatrix(2, [0, 1, 2], [0, 1], [1.0])

    def test_non_finite_data(self):
        with self.assertRaises(InvalidCsrError):
            CSRMatrix(2, [0, 1, 2], [0, 1], [1.0, float("nan")])
        with self.assertRaises(InvalidCsrError):
            CSRMatrix(2, [0, 1, 2], [0, 1], [1.0, float("inf")])

    def test_value_above_abs_limit(self):
        from sparse_cg.limits import MAX_ABS_VALUE
        with self.assertRaises(InvalidCsrError):
            CSRMatrix(1, [0, 1], [0], [2.0 * MAX_ABS_VALUE])

    def test_float_indices_rejected(self):
        with self.assertRaises(InvalidCsrError):
            CSRMatrix(2, [0, 1, 2], [0.0, 1.0], [1.0, 2.0])

    def test_empty_rows_allowed(self):
        # Row 1 has no entries at all: legal sparse structure.
        A = CSRMatrix(3, [0, 1, 1, 2], [0, 2], [5.0, 7.0])
        np.testing.assert_allclose(A.matvec(np.array([1.0, 2.0, 3.0])),
                                   [5.0, 0.0, 21.0])

    def test_n_above_limit_rejected(self):
        from sparse_cg.limits import MAX_N
        n = MAX_N + 1
        with self.assertRaises(InvalidCsrError):
            CSRMatrix(n, [0] * (n + 1), [], [])


class TestSymmetryCheck(unittest.TestCase):

    def test_symmetric_passes(self):
        A = CSRMatrix(
            3, [0, 2, 4, 6], [0, 1, 0, 2, 1, 2],
            [2.0, -1.0, -1.0, -1.0, -1.0, 2.0],
        )
        # Must not raise.
        A.assert_symmetric()

    def test_pattern_asymmetry_raises_with_position(self):
        from sparse_cg import NonSymmetricMatrixError
        # A[0,1] exists, A[1,0] does not.
        A = CSRMatrix(2, [0, 2, 3], [0, 1, 1], [2.0, -1.0, 2.0])
        with self.assertRaises(NonSymmetricMatrixError) as cm:
            A.assert_symmetric()
        self.assertEqual(cm.exception.position, (0, 1))
        self.assertLess(cm.exception.max_abs_diff, 0.0)

    def test_value_asymmetry_raises(self):
        from sparse_cg import NonSymmetricMatrixError
        A = CSRMatrix(
            3, [0, 2, 4, 6],
            [0, 1, 0, 2, 1, 2],
            [4.0, -1.0, -1.5, 4.0, -1.0, 4.0],
        )
        with self.assertRaises(NonSymmetricMatrixError):
            A.assert_symmetric()

    def test_value_asymmetry_within_tolerance_passes(self):
        # Tiny 1e-13 mismatch vs atol=1e-10: passes.
        A = CSRMatrix(
            2, [0, 2, 4], [0, 1, 0, 1], [2.0, -1.0, -1.0 + 1e-13, 2.0]
        )
        A.assert_symmetric(rtol=1e-10, atol=1e-10)


if __name__ == "__main__":
    unittest.main(verbosity=2)
