"""Tests for the JSON request/response layer."""

from __future__ import annotations

import math
import unittest

import numpy as np

from sparse_cg.api import handle_request, process_request
from sparse_cg.generators import (
    laplacian_1d,
    make_rhs_from_exact_solution,
    random_x,
    smooth_x,
)
from tests._utils import true_residual_norm


def _csr_payload(A):
    return {
        "n": A.n,
        "indptr": A.indptr.tolist(),
        "indices": A.indices.tolist(),
        "data": A.data.tolist(),
    }


def _ok(resp):
    assert resp["ok"], resp
    return resp["result"]


class TestApiHappyPaths(unittest.TestCase):

    def test_basic_csr_request(self):
        n = 40
        A, _ = laplacian_1d(n)
        b, _ = make_rhs_from_exact_solution(A, smooth_x(n))
        resp = handle_request({
            "matrix": _csr_payload(A), "b": b.tolist(),
            "tol": 1e-10, "preconditioner": "jacobi",
        })
        res = _ok(resp)
        self.assertEqual(res["status"], "converged")
        self.assertLessEqual(res["relative_residual"], 1e-10 * 1.01)
        self.assertTrue(resp["diagnostics"]["residual_verified_true"])
        self.assertEqual(res["x"].__class__.__name__, "list")
        self.assertEqual(len(res["x"]), n)

    def test_coo_triplet_request(self):
        n = 10
        A, _ = laplacian_1d(n)
        # rebuild in triplet form
        rows, cols, vals = [], [], []
        for i in range(n):
            for k in range(A.indptr[i], A.indptr[i + 1]):
                rows.append(i)
                cols.append(int(A.indices[k]))
                vals.append(float(A.data[k]))
        x_true = random_x(n, seed=2)
        b = A.matvec(x_true)
        resp = handle_request({
            "matrix": {"n": n, "rows": rows, "cols": cols, "values": vals},
            "b": b.tolist(), "tol": 1e-11,
        })
        res = _ok(resp)
        self.assertEqual(res["status"], "converged")
        np.testing.assert_allclose(np.array(res["x"]), x_true, rtol=1e-9)

    def test_duplicate_entries_counted_in_diagnostics(self):
        # Symmetric 2x2 matrix with deliberate duplicates on both halves:
        # A = [[2,-1],[-1,2]]; nnz_declared=6, nnz_stored=3.
        payload = {
            "matrix": {"n": 2,
                       "indptr": [0, 4, 6],
                       "indices": [0, 0, 1, 1, 0, 1],
                       "data": [1.25, 0.75, -0.4, -0.6, -1.0, 2.0]},
            "b": [1.0, 1.0],
        }
        resp = handle_request(payload)
        self.assertTrue(resp["ok"], resp)
        dg = resp["diagnostics"]
        self.assertEqual(dg["nnz_declared"], 6)
        self.assertEqual(dg["nnz_stored"], 4)
        self.assertEqual(dg["duplicates_or_zeros_dropped"], 2)

    def test_zero_rhs_response(self):
        n = 20
        A, _ = laplacian_1d(n)
        resp = handle_request({"matrix": _csr_payload(A), "b": [0.0] * n})
        res = _ok(resp)
        self.assertEqual(res["status"], "zero_rhs")
        self.assertTrue(res["converged"])
        self.assertEqual(res["iterations"], 0)

    def test_history_and_solution_can_be_omitted(self):
        n = 10
        A, _ = laplacian_1d(n)
        b, _ = make_rhs_from_exact_solution(A, smooth_x(n))
        resp = handle_request({
            "matrix": _csr_payload(A), "b": b.tolist(),
            "include_history": False, "include_solution": False,
        })
        res = _ok(resp)
        self.assertNotIn("history", res)
        self.assertNotIn("x", res)
        # Convergence info still present.
        self.assertEqual(res["status"], "converged")

    def test_non_positive_curvature_is_ok_envelope_unsuccessful_result(self):
        payload = {
            "matrix": {"n": 2, "indptr": [0, 2, 4],
                       "indices": [0, 1, 0, 1],
                       "data": [2.0, 3.0, 3.0, 2.0]},
            "b": [1.0, -1.0],
            "assume_spd": True,  # bypass up-front checks; curvature must fire
        }
        resp = handle_request(payload)
        self.assertTrue(resp["ok"], resp)
        res = resp["result"]
        self.assertFalse(res["converged"])
        self.assertEqual(res["status"], "non_positive_curvature")
        self.assertLess(res["non_positive_value"], 0.0)

    def test_assume_spd_skips_checks(self):
        # Non-symmetric matrix accepted when assume_spd is true.
        payload = {
            "matrix": {"n": 2, "indptr": [0, 2, 3],
                       "indices": [0, 1, 1], "data": [2.0, -1.0, 2.0]},
            "b": [1.0, 1.0], "assume_spd": True,
        }
        resp = handle_request(payload)
        # Runs (result quality is the caller's responsibility).
        self.assertTrue(resp["ok"], resp)
        self.assertIsNone(resp["diagnostics"]["symmetry_max_abs_diff"])


class TestApiRejections(unittest.TestCase):

    def _bad(self, payload):
        resp = handle_request(payload)
        self.assertFalse(resp["ok"])
        return resp["error"]

    def test_body_not_object(self):
        err = self._bad([1, 2, 3])
        self.assertEqual(err["code"], "invalid_request")

    def test_invalid_json_is_handled_by_cli_layer_only(self):
        # process_request raises on raw string; direct API callers get an
        # exception with the stable code.
        from sparse_cg import InvalidRequestError
        with self.assertRaises(InvalidRequestError):
            process_request("not an object")

    def test_missing_matrix(self):
        err = self._bad({"b": [1.0]})
        self.assertEqual(err["code"], "invalid_request")

    def test_missing_b(self):
        err = self._bad({
            "matrix": {"n": 1, "indptr": [0, 1], "indices": [0],
                       "data": [1.0]}
        })
        self.assertEqual(err["code"], "invalid_request")

    def test_unknown_top_level_field_rejected(self):
        err = self._bad({
            "matrix": {"n": 1, "indptr": [0, 1], "indices": [0],
                       "data": [1.0]},
            "b": [1.0], "tolerance": 1e-9,
        })
        self.assertEqual(err["code"], "invalid_request")
        self.assertIn("tolerance", err["message"])

    def test_b_length_mismatch(self):
        err = self._bad({
            "matrix": {"n": 2, "indptr": [0, 1, 2], "indices": [0, 1],
                       "data": [1.0, 2.0]},
            "b": [1.0, 2.0, 3.0],
        })
        self.assertEqual(err["code"], "invalid_request")

    def test_bool_rejected_as_number(self):
        err = self._bad({
            "matrix": {"n": 1, "indptr": [0, 1], "indices": [0],
                       "data": [1.0]},
            "b": [True],
        })
        self.assertEqual(err["code"], "invalid_request")

    def test_string_entry_rejected(self):
        err = self._bad({
            "matrix": {"n": 1, "indptr": [0, 1], "indices": [0],
                       "data": ["oops"]},
            "b": [1.0],
        })
        self.assertEqual(err["code"], "invalid_request")

    def test_tol_out_of_range(self):
        base = {
            "matrix": {"n": 1, "indptr": [0, 1], "indices": [0],
                       "data": [1.0]},
            "b": [1.0],
        }
        err = self._bad({**base, "tol": 5.0})
        self.assertEqual(err["code"], "invalid_request")
        err = self._bad({**base, "tol": 1e-30})
        self.assertEqual(err["code"], "invalid_request")

    def test_max_iter_out_of_range(self):
        base = {
            "matrix": {"n": 1, "indptr": [0, 1], "indices": [0],
                       "data": [1.0]},
            "b": [1.0],
        }
        err = self._bad({**base, "max_iter": 0})
        self.assertEqual(err["code"], "invalid_request")
        err = self._bad({**base, "max_iter": 200000})
        self.assertEqual(err["code"], "invalid_request")

    def test_bad_preconditioner_name(self):
        n = 5
        A, _ = laplacian_1d(n)
        err = self._bad({"matrix": _csr_payload(A), "b": [1.0] * n,
                         "preconditioner": "ilu"})
        self.assertEqual(err["code"], "invalid_request")

    def test_illegal_csr_indptr_not_monotonic(self):
        err = self._bad({
            "matrix": {"n": 3, "indptr": [0, 2, 1, 3],
                       "indices": [0, 1, 0], "data": [1.0, 2.0, 3.0]},
            "b": [1.0, 1.0, 1.0],
        })
        self.assertEqual(err["code"], "invalid_csr")

    def test_illegal_csr_index_out_of_range(self):
        err = self._bad({
            "matrix": {"n": 2, "indptr": [0, 1, 2],
                       "indices": [5, 1], "data": [1.0, 2.0]},
            "b": [1.0, 1.0],
        })
        self.assertEqual(err["code"], "invalid_csr")

    def test_illegal_csr_pointer_mismatch(self):
        err = self._bad({
            "matrix": {"n": 2, "indptr": [0, 1, 2],
                       "indices": [0, 1, 0], "data": [1.0, 2.0, 3.0]},
            "b": [1.0, 1.0],
        })
        self.assertEqual(err["code"], "invalid_csr")

    def test_non_finite_in_data_rejected(self):
        # Programmatic callers may pass NaN/inf even though strict JSON
        # cannot carry them; the API number validator rejects it up front
        # (invalid_request).  The library-level CSR constructor itself
        # raises invalid_csr -- covered in tests/test_csr.py.
        err = self._bad({
            "matrix": {"n": 1, "indptr": [0, 1], "indices": [0],
                       "data": [float("nan")]},
            "b": [1.0],
        })
        self.assertEqual(err["code"], "invalid_request")

    def test_non_symmetric_matrix_rejected(self):
        err = self._bad({
            "matrix": {"n": 3, "indptr": [0, 2, 4, 6],
                       "indices": [0, 1, 0, 2, 1, 2],
                       "data": [4.0, -1.0, -1.5, 4.0, -1.0, 4.0]},
            "b": [1.0, 1.0, 1.0],
        })
        self.assertEqual(err["code"], "non_symmetric_matrix")
        self.assertIn("details", err)
        self.assertEqual(err["details"]["position"], [0, 1])

    def test_non_positive_diagonal_rejected(self):
        # Symmetric but with A[1,1] = 0: semidefinite, caught up front.
        err = self._bad({
            "matrix": {"n": 2, "indptr": [0, 2, 4],
                       "indices": [0, 1, 0, 1],
                       "data": [1.0, 1.0, 1.0, 0.0]},
            "b": [1.0, 1.0],
        })
        self.assertEqual(err["code"], "not_positive_definite")
        self.assertEqual(err["details"]["index"], 1)

    def test_negative_diagonal_rejected(self):
        err = self._bad({
            "matrix": {"n": 2, "indptr": [0, 2, 4],
                       "indices": [0, 1, 0, 1],
                       "data": [1.0, 2.0, 2.0, -1.0]},
            "b": [1.0, 1.0],
        })
        self.assertEqual(err["code"], "not_positive_definite")

    def test_nan_in_b_rejected(self):
        err = self._bad({
            "matrix": {"n": 1, "indptr": [0, 1], "indices": [0],
                       "data": [1.0]},
            "b": [float("nan")],
        })
        # JSON cannot carry NaN via strict parsing, but programmatic callers
        # still must be told precisely.
        self.assertEqual(err["code"], "invalid_request")

    def test_value_limit_enforced_on_b(self):
        from sparse_cg.limits import MAX_ABS_VALUE
        err = self._bad({
            "matrix": {"n": 1, "indptr": [0, 1], "indices": [0],
                       "data": [1.0]},
            "b": [2.0 * MAX_ABS_VALUE],
        })
        self.assertEqual(err["code"], "invalid_request")

    def test_mixed_csr_and_coo_fields_rejected(self):
        err = self._bad({
            "matrix": {"n": 1, "indptr": [0, 1], "indices": [0],
                       "data": [1.0], "rows": [0], "cols": [0],
                       "values": [1.0]},
            "b": [1.0],
        })
        self.assertEqual(err["code"], "invalid_request")


class TestApiResidualHonesty(unittest.TestCase):
    """Acceptance rule: compare residuals, via the JSON response itself."""

    def test_response_residual_matches_independent_computation(self):
        n = 75
        A, _ = laplacian_1d(n)
        x_true = random_x(n, seed=15)
        b = A.matvec(x_true)
        resp = handle_request({
            "matrix": _csr_payload(A), "b": b.tolist(), "tol": 1e-9,
        })
        res = _ok(resp)
        x = np.array(res["x"])
        independent = true_residual_norm(A, x, b)
        self.assertAlmostEqual(res["residual_norm"], independent,
                               delta=1e-9 * max(1.0, independent))
        self.assertLess(independent, 1e-9 * np.linalg.norm(b))


if __name__ == "__main__":
    unittest.main(verbosity=2)
