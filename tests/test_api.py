"""JSON API 层测试：solve_request 的端到端行为与错误码。"""

from __future__ import annotations

import unittest

import numpy as np

from sparse_cg import CSRMatrix, solve_request


def _lap_dense(n: int) -> np.ndarray:
    d = 2.0 * np.eye(n)
    if n > 1:
        d += -np.eye(n, k=1) + -np.eye(n, k=-1)
    return d


class TestAPISuccess(unittest.TestCase):
    def test_end_to_end_known_solution(self) -> None:
        n = 60
        a = CSRMatrix.from_dense(_lap_dense(n))
        rng = np.random.default_rng(1)
        x_exact = rng.standard_normal(n)
        b = _lap_dense(n) @ x_exact
        resp = solve_request(
            {
                "matrix": {
                    "n": n,
                    "data": [float(v) for v in a.data],
                    "indices": [int(v) for v in a.indices],
                    "indptr": [int(v) for v in a.indptr],
                },
                "b": [float(v) for v in b],
                "rtol": 1e-10,
            }
        )
        self.assertTrue(resp["ok"], resp)
        self.assertEqual(resp["status"], "converged", resp["message"])
        self.assertFalse(resp["algorithm_failed"])
        # 核心验收：用真实残差比较，且与已知解比较
        x = np.asarray(resp["x"])
        self.assertLess(np.linalg.norm(b - _lap_dense(n) @ x), 1e-9)
        self.assertLess(np.linalg.norm(x - x_exact), 1e-7)
        self.assertEqual(resp["n"], n)
        self.assertEqual(resp["nnz"], 3 * n - 2)

    def test_zero_rhs(self) -> None:
        resp = solve_request(
            {
                "matrix": {"n": 2, "data": [1.0, 2.0], "indices": [0, 1],
                           "indptr": [0, 1, 2]},
                "b": [0.0, 0.0],
            }
        )
        self.assertTrue(resp["ok"])
        self.assertEqual(resp["status"], "converged")
        self.assertEqual(resp["x"], [0.0, 0.0])
        self.assertEqual(resp["iterations"], 0)

    def test_preconditioner_none(self) -> None:
        n = 30
        a = CSRMatrix.from_dense(_lap_dense(n))
        b = np.ones(n)
        resp = solve_request(
            {
                "matrix": {
                    "n": n,
                    "data": [float(v) for v in a.data],
                    "indices": [int(v) for v in a.indices],
                    "indptr": [int(v) for v in a.indptr],
                },
                "b": [float(v) for v in b],
                "preconditioner": "none",
            }
        )
        self.assertTrue(resp["ok"])
        self.assertEqual(resp["status"], "converged")

    def test_algorithm_failure_reported_not_errored(self) -> None:
        # 不定矩阵（对角为正，过前置检查）：ok=true，但 status 是失败诊断
        resp = solve_request(
            {
                "matrix": {
                    "n": 2,
                    "data": [1.0, 2.0, 2.0, 1.0],
                    "indices": [0, 1, 0, 1],
                    "indptr": [0, 2, 4],
                },
                "b": [1.0, 0.0],
                "preconditioner": "none",
            }
        )
        self.assertTrue(resp["ok"], resp)
        self.assertEqual(resp["status"], "negative_curvature")
        self.assertTrue(resp["algorithm_failed"])
        self.assertFalse(resp["converged"])

    def test_response_is_json_serializable(self) -> None:
        import json

        resp = solve_request(
            {
                "matrix": {"n": 1, "data": [2.0], "indices": [0], "indptr": [0, 1]},
                "b": [3.0],
            }
        )
        s = json.dumps(resp, ensure_ascii=False)
        self.assertIn("converged", s)
        self.assertAlmostEqual(resp["x"][0], 1.5)


class TestAPIInvalidCSR(unittest.TestCase):
    def _expect(self, payload: dict, code: str) -> None:
        resp = solve_request(payload)
        self.assertFalse(resp["ok"])
        self.assertEqual(resp["error"]["code"], code, resp["error"])

    def _mat(self, **kw) -> dict:
        base = {
            "n": 2,
            "data": [2.0, -1.0, -1.0, 2.0],
            "indices": [0, 1, 0, 1],
            "indptr": [0, 2, 4],
        }
        base.update(kw)
        return {"matrix": base, "b": [1.0, 1.0]}

    def test_index_out_of_bounds(self) -> None:
        self._expect(self._mat(indices=[0, 5, 0, 1]), "index_out_of_bounds")

    def test_duplicate_column(self) -> None:
        self._expect(
            self._mat(
                n=1,
                data=[1.0, 1.0],
                indices=[0, 0],
                indptr=[0, 2],
            ),
            "indices_not_sorted",
        )

    def test_unsorted_column(self) -> None:
        self._expect(
            self._mat(data=[-1.0, 2.0, -1.0, 2.0], indices=[1, 0, 0, 1]),
            "indices_not_sorted",
        )

    def test_indptr_bad(self) -> None:
        self._expect(self._mat(indptr=[1, 2, 4]), "indptr_invalid")
        # 终点 2 != nnz(4)；行内内容仍合法，应报 indptr_invalid
        self._expect(self._mat(indptr=[0, 1, 2]), "indptr_invalid")
        # 内部倒退（新检查顺序下先于终点检查）
        self._expect(self._mat(indptr=[0, 4, 2]), "indptr_not_monotonic")

    def test_length_mismatch(self) -> None:
        self._expect(self._mat(data=[2.0, -1.0, -1.0]), "indices_length_mismatch")
        self._expect(self._mat(indptr=[0, 2, 4, 4]), "indptr_length_mismatch")

    def test_float_index_rejected(self) -> None:
        self._expect(self._mat(indices=[0.0, 1.0, 0, 1]), "indices_not_integer")

    def test_nan_rejected(self) -> None:
        # Python float('nan')：API 层在数值有限性处拒绝
        self._expect(self._mat(data=[2.0, float("nan"), -1.0, 2.0]), "data_not_finite")

    def test_n_too_large(self) -> None:
        self._expect(
            {"matrix": {"n": 10001, "data": [], "indices": [], "indptr": [0]},
             "b": []},
            "n_too_large",
        )

    def test_n_negative(self) -> None:
        self._expect(
            {"matrix": {"n": -1, "data": [], "indices": [], "indptr": []}, "b": []},
            "n_negative",
        )


class TestAPIInvalidSemantics(unittest.TestCase):
    def _expect(self, payload: dict, code: str) -> None:
        resp = solve_request(payload)
        self.assertFalse(resp["ok"])
        self.assertEqual(resp["error"]["code"], code, resp["error"])

    SYM = {
        "n": 2,
        "data": [2.0, -1.0, -1.0, 2.0],
        "indices": [0, 1, 0, 1],
        "indptr": [0, 2, 4],
    }

    def test_b_length_mismatch(self) -> None:
        self._expect({"matrix": self.SYM, "b": [1.0, 2.0, 3.0]}, "b_length_mismatch")

    def test_b_not_numeric(self) -> None:
        self._expect({"matrix": self.SYM, "b": [1.0, "two"]}, "b_not_numeric")

    def test_missing_fields(self) -> None:
        self._expect({"b": [1.0]}, "missing_matrix")
        self._expect({"matrix": self.SYM}, "missing_b")
        self._expect(
            {"matrix": {"n": 2, "data": [], "indices": []}, "b": []},
            "matrix_missing_fields",
        )

    def test_not_object(self) -> None:
        self._expect([1, 2, 3], "request_not_object")
        self._expect("hello", "request_not_object")

    def test_unknown_field(self) -> None:
        self._expect(
            {"matrix": self.SYM, "b": [1.0, 1.0], "solver": "gmres"},
            "unknown_fields",
        )

    def test_bad_rtol(self) -> None:
        self._expect(
            {"matrix": self.SYM, "b": [1.0, 1.0], "rtol": -1e-8}, "invalid_rtol"
        )
        self._expect(
            {"matrix": self.SYM, "b": [1.0, 1.0], "rtol": "1e-8"}, "invalid_rtol"
        )

    def test_bad_max_iter(self) -> None:
        self._expect(
            {"matrix": self.SYM, "b": [1.0, 1.0], "max_iter": -3},
            "invalid_max_iter",
        )
        self._expect(
            {"matrix": self.SYM, "b": [1.0, 1.0], "max_iter": 200000},
            "invalid_max_iter",
        )

    def test_bad_preconditioner(self) -> None:
        self._expect(
            {"matrix": self.SYM, "b": [1.0, 1.0], "preconditioner": "ilu"},
            "invalid_preconditioner",
        )

    def test_non_symmetric_rejected(self) -> None:
        # 结构非对称：(0,1) 存在，(1,0) 缺失
        self._expect(
            {
                "matrix": {
                    "n": 2,
                    "data": [2.0, -1.0, 2.0],
                    "indices": [0, 1, 1],
                    "indptr": [0, 2, 3],
                },
                "b": [1.0, 1.0],
            },
            "matrix_not_symmetric",
        )
        # 数值非对称
        self._expect(
            {
                "matrix": {
                    "n": 2,
                    "data": [2.0, -1.0, -0.5, 2.0],
                    "indices": [0, 1, 0, 1],
                    "indptr": [0, 2, 4],
                },
                "b": [1.0, 1.0],
            },
            "matrix_not_symmetric",
        )

    def test_non_positive_diagonal_rejected(self) -> None:
        # 对称但对角元为负（必然非正定）——前置检查直接拒绝
        self._expect(
            {
                "matrix": {
                    "n": 2,
                    "data": [-1.0, 0.5, 0.5, -1.0],
                    "indices": [0, 1, 0, 1],
                    "indptr": [0, 2, 4],
                },
                "b": [1.0, 1.0],
            },
            "non_positive_diagonal",
        )

    def test_diagonal_missing_rejected(self) -> None:
        # 结构对称但无对角元
        self._expect(
            {
                "matrix": {
                    "n": 2,
                    "data": [1.0, 1.0],
                    "indices": [1, 0],
                    "indptr": [0, 1, 2],
                },
                "b": [1.0, 1.0],
            },
            "diagonal_missing",
        )

    def test_none_preconditioner_allows_negative_diag_check_still(self) -> None:
        # 即使 preconditioner=none，SPD 前置检查（对称+正对角）仍执行
        self._expect(
            {
                "matrix": {
                    "n": 2,
                    "data": [-1.0, 0.5, 0.5, -1.0],
                    "indices": [0, 1, 0, 1],
                    "indptr": [0, 2, 4],
                },
                "b": [1.0, 1.0],
                "preconditioner": "none",
            },
            "non_positive_diagonal",
        )


if __name__ == "__main__":
    unittest.main()
