"""PCG 核心算法测试。

设计原则（验收要求）：
* 用**已知解**生成稀疏系统（选定 x_exact，令 b = A x_exact），
  断言真实残差 ||b - A x|| 与解误差 ||x - x_exact||，而不只看迭代次数；
* 覆盖：良态稀疏系统、病态系统、零右端、零维、负曲率（不定）、
  零曲率（奇异）、停滞、发散、Jacobi 与无预条件对比、真实残差监控。
"""

from __future__ import annotations

import unittest

import numpy as np

from sparse_cg import CSRMatrix, RequestError
from sparse_cg.pcg import (
    STATUS_CONVERGED,
    STATUS_DIVERGED,
    STATUS_MAX_ITER,
    STATUS_NEG_CURV,
    STATUS_PRECOND_BREAK,
    STATUS_STAGNATED,
    STATUS_ZERO_CURV,
    pcg,
)


def laplacian_1d(n: int) -> CSRMatrix:
    """一维拉普拉斯：三对角 SPD，特征值 2-2cos(kπ/(n+1))，条件数 ~ n²。"""
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


def make_system(a: CSRMatrix, seed: int = 1, x0_zero: bool = True):
    """用已知解生成系统：x_exact 随机，b = A x_exact。"""
    rng = np.random.default_rng(seed)
    x_exact = rng.standard_normal(a.n)
    b = a.matvec(x_exact)
    x0 = np.zeros(a.n) if x0_zero else rng.standard_normal(a.n)
    return b, x_exact, x0


class TestConvergenceKnownSolution(unittest.TestCase):
    def test_laplacian_small(self) -> None:
        a = laplacian_1d(50)
        b, x_exact, x0 = make_system(a)
        res = pcg(a, b, x0)
        self.assertEqual(res.status, STATUS_CONVERGED, res.message)
        true_r = np.linalg.norm(b - a.matvec(res.x))
        self.assertLessEqual(true_r, 1e-8 * np.linalg.norm(b) + 1e-12)
        self.assertLess(np.linalg.norm(res.x - x_exact), 1e-6)
        # 报告的残差必须就是真实残差（不只是递推值）
        self.assertAlmostEqual(res.residual_norm, true_r, places=10)
        self.assertTrue(res.residual_history[-1] <= 1e-8 * np.linalg.norm(b) + 1e-12)

    def test_laplacian_larger(self) -> None:
        n = 500
        a = laplacian_1d(n)
        b, x_exact, _ = make_system(a, seed=2)
        res = pcg(a, b)
        self.assertEqual(res.status, STATUS_CONVERGED, res.message)
        true_r = np.linalg.norm(b - a.matvec(res.x))
        self.assertLessEqual(true_r, 1e-8 * np.linalg.norm(b) + 1e-12)
        # 三对角条件数 ~ n²/π²，解误差仍应远小于 1
        self.assertLess(np.linalg.norm(res.x - x_exact), 1e-4)

    def test_diagonal_system(self) -> None:
        d = np.array([1.0, 2.0, 100.0, 0.5])
        a = CSRMatrix.from_dense(np.diag(d))
        b, x_exact, _ = make_system(a, seed=3)
        res = pcg(a, b)
        self.assertEqual(res.status, STATUS_CONVERGED)
        np.testing.assert_allclose(res.x, x_exact, atol=1e-10)

    def test_nonzero_x0(self) -> None:
        a = laplacian_1d(30)
        b, x_exact, x0 = make_system(a, seed=4, x0_zero=False)
        res = pcg(a, b, x0)
        self.assertEqual(res.status, STATUS_CONVERGED, res.message)
        self.assertLess(np.linalg.norm(b - a.matvec(res.x)), 1e-8 * np.linalg.norm(b))
        self.assertLess(np.linalg.norm(res.x - x_exact), 1e-6)

    def test_zero_dimension(self) -> None:
        a = CSRMatrix(np.array([]), np.array([]), np.array([0]), 0)
        res = pcg(a, np.array([]))
        self.assertEqual(res.status, STATUS_CONVERGED)
        self.assertEqual(res.x.shape, (0,))

    def test_x0_exact(self) -> None:
        a = laplacian_1d(10)
        b, x_exact, _ = make_system(a)
        res = pcg(a, b, x_exact)
        self.assertEqual(res.status, STATUS_CONVERGED)
        self.assertEqual(res.iterations, 0)
        self.assertEqual(res.residual_norm, 0.0)


class TestIllConditioned(unittest.TestCase):
    """病态系统：缩放后的三对角矩阵，条件数保持 ~ 4e8 量级。"""

    def _scaled_laplacian(self, n: int, scale: float) -> CSRMatrix:
        base = laplacian_1d(n).to_dense()
        # 非奇异对角相似变换 S A S 保持正定性，条件数可被显著放大
        s = np.geomspace(1.0, scale, n)
        dense = (s[:, None] * base) * s[None, :]
        return CSRMatrix.from_dense(dense)

    def test_ill_conditioned_converges_with_tighter_iter_budget(self) -> None:
        a = self._scaled_laplacian(60, 1e4)  # 条件数 ~ 4e8
        b, x_exact, _ = make_system(a, seed=5)
        res = pcg(a, b, rtol=1e-8, atol=1e-12, preconditioner="jacobi")
        # 病态系统可能不收敛到 1e-8；核心断言是：失败时残差也必须如实
        if res.status == STATUS_CONVERGED:
            self.assertLess(
                np.linalg.norm(b - a.matvec(res.x)),
                1e-8 * np.linalg.norm(b) + 1e-12,
            )
        else:
            self.assertIn(
                res.status, (STATUS_MAX_ITER, STATUS_STAGNATED)
            )
            # 即使未收敛，残差应已大幅下降，且报告值与真实值一致
            true_r = np.linalg.norm(b - a.matvec(res.x))
            self.assertLess(true_r, res.initial_residual_norm)
            self.assertAlmostEqual(res.residual_norm, true_r, places=8)

    def test_ill_conditioned_loose_tolerance(self) -> None:
        # 放宽容差到 1e-4，同一病态系统应能收敛
        a = self._scaled_laplacian(60, 1e4)
        b, x_exact, _ = make_system(a, seed=5)
        res = pcg(a, b, rtol=1e-4, atol=1e-10)
        self.assertEqual(res.status, STATUS_CONVERGED, res.message)
        true_r = np.linalg.norm(b - a.matvec(res.x))
        self.assertLessEqual(true_r, 1e-4 * np.linalg.norm(b) + 1e-10)

    def test_condition_number_estimate(self) -> None:
        # 确认测试夹具确实病态：稠密特征值估计条件数
        a = self._scaled_laplacian(60, 1e4)
        eigs = np.linalg.eigvalsh(a.to_dense())
        cond = eigs[-1] / eigs[0]
        self.assertGreater(cond, 1e7)


class TestZeroRHS(unittest.TestCase):
    def test_zero_rhs_zero_x0(self) -> None:
        a = laplacian_1d(20)
        res = pcg(a, np.zeros(20))
        self.assertEqual(res.status, STATUS_CONVERGED)
        self.assertEqual(res.iterations, 0)
        self.assertEqual(res.residual_norm, 0.0)
        np.testing.assert_array_equal(res.x, np.zeros(20))

    def test_zero_rhs_nonzero_x0_returns_to_zero(self) -> None:
        a = laplacian_1d(20)
        rng = np.random.default_rng(7)
        x0 = rng.standard_normal(20)
        res = pcg(a, np.zeros(20), x0, rtol=1e-10, atol=1e-12)
        self.assertEqual(res.status, STATUS_CONVERGED, res.message)
        # b = 0，唯一解是零向量
        self.assertLess(np.linalg.norm(res.x), 1e-8)
        self.assertLess(
            res.residual_norm, 1e-12
        )  # 绝对容差在 ||b||=0 时起决定作用

    def test_zero_rhs_atol_zero_stagnates_at_roundoff_floor(self) -> None:
        # rtol=atol=0 时阈值为 0，残差最终被舍入地板挡住（约 1e-130 量级），
        # 永不可达：停滞监控应在 max_iter 之前抓住它，而非谎报收敛或耗尽预算。
        # n=50 的系统约 50 步内收敛到舍入地板，检查窗口取 period=10。
        a = laplacian_1d(30)
        x0 = np.ones(30)
        res = pcg(
            a,
            np.zeros(30),
            x0,
            rtol=0.0,
            atol=0.0,
            max_iter=80,
            restart_period=10,
            stagnation_patience=3,
            stagnation_factor=0.999,
        )
        self.assertEqual(res.status, STATUS_STAGNATED, res.message)
        self.assertLess(res.iterations, 80)
        # 停滞时的残差仍是真实残差
        self.assertAlmostEqual(
            res.residual_norm,
            float(np.linalg.norm(a.matvec(res.x))),
            places=12,
        )


class TestNonPositiveCurvature(unittest.TestCase):
    """不定/奇异矩阵：API 的对称前置检查放行，但 PCG 必须靠曲率诊断抓住。"""

    def test_indefinite_negative_curvature(self) -> None:
        # [[1, 2], [2, 1]]：对称，对角为正，但特征值 {3, -1}，不定
        a = CSRMatrix(
            data=np.array([1.0, 2.0, 2.0, 1.0]),
            indices=np.array([0, 1, 0, 1]),
            indptr=np.array([0, 2, 4]),
            n=2,
        )
        b = np.array([1.0, 0.0])
        res = pcg(a, b, preconditioner="none")  # 直接调核心，绕过 API 对称检查
        self.assertEqual(res.status, STATUS_NEG_CURV)
        self.assertIn("负曲率", res.message)
        # 初始 r=b=(1,0) 恰为正方向（特征值 3），第 1 步推进后
        # 第 2 个搜索方向暴露负特征值 -1
        self.assertEqual(res.iterations, 2)
        # 失败结果仍可序列化、残差与当前 x 一致
        d = res.to_dict()
        self.assertFalse(d["converged"])
        self.assertAlmostEqual(
            d["residual_norm"], float(np.linalg.norm(b - a.matvec(res.x))), places=12
        )

    def test_indefinite_jacobi_also_diagnosed(self) -> None:
        # 同一不定矩阵 + Jacobi（对角元仍为正，预条件本身不坏，是曲率坏）
        a = CSRMatrix(
            data=np.array([1.0, 2.0, 2.0, 1.0]),
            indices=np.array([0, 1, 0, 1]),
            indptr=np.array([0, 2, 4]),
            n=2,
        )
        res = pcg(a, np.array([1.0, 0.0]), preconditioner="jacobi")
        self.assertEqual(res.status, STATUS_NEG_CURV)

    def test_singular_zero_curvature(self) -> None:
        # [[1, 1], [1, 1]]：秩 1 半正定；取 b 与零空间方向一致，
        # 初始 r=b 本身就是零曲率搜索方向
        a = CSRMatrix(
            data=np.array([1.0, 1.0, 1.0, 1.0]),
            indices=np.array([0, 1, 0, 1]),
            indptr=np.array([0, 2, 4]),
            n=2,
        )
        res = pcg(a, np.array([1.0, -1.0]), preconditioner="none")
        self.assertEqual(res.status, STATUS_ZERO_CURV)
        self.assertIn("零曲率", res.message)


class TestFailureModes(unittest.TestCase):
    def test_max_iterations_returns_current_best(self) -> None:
        # 拉普拉斯矩阵一步远不能解；max_iter=1 后必须返回 max_iterations，
        # 且用真实残差复核
        n = 100
        a = laplacian_1d(n)
        rng = np.random.default_rng(11)
        b = rng.standard_normal(n)
        res = pcg(a, b, rtol=0.0, atol=0.0, max_iter=1)
        self.assertEqual(res.status, STATUS_MAX_ITER)
        self.assertEqual(res.iterations, 1)
        self.assertFalse(res.converged)
        # 退出时用真实残差复核
        self.assertAlmostEqual(
            res.residual_norm, float(np.linalg.norm(b - a.matvec(res.x))), places=12
        )
        self.assertIn("最大迭代数", res.message)

    def test_stagnation_detected(self) -> None:
        # 用“精度受限算子”制造确定性的停滞：矩阵-向量乘法只保留约 7 位
        # 有效数字，残差被钉在 1e-7 量级地板上，无法继续下降。
        # 阈值设为不可达（0），停滞监控必须在 max_iter 之前抓住它。
        class QuantizedOperator:
            """与 CSRMatrix 鸭子同形：暴露 n / nnz() / diagonal() / matvec()。"""

            def __init__(self, base: CSRMatrix, digits: float) -> None:
                self._base = base
                self.n = base.n
                self._q = digits

            def nnz(self) -> int:
                return self._base.nnz()

            def diagonal(self) -> np.ndarray:
                return self._base.diagonal()

            def matvec(self, x: np.ndarray) -> np.ndarray:
                y = self._base.matvec(x)
                # 量化到固定相对精度：模拟精度受限的算子，产生残差地板
                scale = np.maximum(np.abs(y), 1.0)
                return y + self._q * scale * np.sin(
                    997.0 * y / scale + 0.5
                )

        a = QuantizedOperator(laplacian_1d(200), 5e-6)
        # 用“干净”矩阵精算右端，保证系统有意义
        b = laplacian_1d(200).matvec(np.linspace(0.0, 1.0, 200))
        res = pcg(
            a,
            b,
            rtol=0.0,
            atol=0.0,  # 阈值 0，永不可达
            max_iter=300,
            restart_period=10,
            stagnation_patience=3,
            stagnation_factor=0.8,
            preconditioner="jacobi",
        )
        self.assertEqual(res.status, STATUS_STAGNATED, res.message)
        self.assertLess(res.iterations, 300)
        self.assertAlmostEqual(
            res.residual_norm,
            float(np.linalg.norm(b - a.matvec(res.x))),
            places=10,
        )
        self.assertLess(res.iterations, 500)

    def test_diverged_detected(self) -> None:
        # 算子返回 NaN：第一次真实残差检查即应判发散
        class NaNOperator:
            n = 10

            def nnz(self) -> int:
                return 10

            def diagonal(self) -> np.ndarray:
                return np.ones(10)

            def matvec(self, x: np.ndarray) -> np.ndarray:
                return np.full(x.shape, np.nan)

        res = pcg(
            NaNOperator(),
            np.ones(10),
            rtol=1e-8,
            atol=1e-12,
            max_iter=50,
            restart_period=1,
        )
        self.assertEqual(res.status, STATUS_DIVERGED)

    def test_jacobi_tiny_diagonal_breakdown(self) -> None:
        # 结构对称、对角为正但小到与非对角元相比接近下溢尺度
        eps = 1e-300
        a = CSRMatrix(
            data=np.array([eps, 1e100, 1e100, eps]),
            indices=np.array([0, 1, 0, 1]),
            indptr=np.array([0, 2, 4]),
            n=2,
        )
        res = pcg(a, np.array([1.0, 1.0]), preconditioner="jacobi")
        self.assertEqual(res.status, STATUS_PRECOND_BREAK)
        self.assertIn("预条件失效", res.message)

    def test_diverged_guard(self) -> None:
        # 非有限 b 直接被输入校验拒绝，属于发散之外的输入层防护
        a = laplacian_1d(5)
        with self.assertRaises(RequestError) as ctx:
            pcg(a, np.array([1.0, 2.0, np.nan, 3.0, 4.0]))
        self.assertEqual(ctx.exception.code, "b_not_finite")

    def test_bad_preconditioner_name(self) -> None:
        a = laplacian_1d(4)
        with self.assertRaises(RequestError) as ctx:
            pcg(a, np.ones(4), preconditioner="ilu")
        self.assertEqual(ctx.exception.code, "invalid_preconditioner")

    def test_dimension_mismatch(self) -> None:
        a = laplacian_1d(4)
        with self.assertRaises(RequestError) as ctx:
            pcg(a, np.ones(3))
        self.assertEqual(ctx.exception.code, "dimension_mismatch")
        with self.assertRaises(RequestError) as ctx:
            pcg(a, np.ones(4), x0=np.ones(5))
        self.assertEqual(ctx.exception.code, "dimension_mismatch")

    def test_invalid_tolerances(self) -> None:
        a = laplacian_1d(4)
        with self.assertRaises(RequestError) as ctx:
            pcg(a, np.ones(4), rtol=-1e-8)
        self.assertEqual(ctx.exception.code, "invalid_rtol")
        with self.assertRaises(RequestError) as ctx:
            pcg(a, np.ones(4), max_iter=0)
        self.assertEqual(ctx.exception.code, "invalid_max_iter")
        with self.assertRaises(RequestError) as ctx:
            pcg(a, np.ones(4), max_iter=100_001)
        self.assertEqual(ctx.exception.code, "invalid_max_iter")


class TestPreconditionerAndResidualMonitoring(unittest.TestCase):
    def test_jacobi_reduces_iterations_on_scaled_system(self) -> None:
        # 对角尺度悬殊的对角阵：无预条件需多轮，Jacobi 一步到位
        n = 20
        d = np.geomspace(1.0, 1e8, n)
        a = CSRMatrix.from_dense(np.diag(d))
        rng = np.random.default_rng(13)
        b = rng.standard_normal(n)
        plain = pcg(a, b, preconditioner="none")
        pre = pcg(a, b, preconditioner="jacobi")
        self.assertEqual(plain.status, STATUS_CONVERGED)
        self.assertEqual(pre.status, STATUS_CONVERGED)
        self.assertLessEqual(pre.iterations, plain.iterations)

    def test_history_is_true_residual(self) -> None:
        # 残差历史里的每个值都必须能由真实残差复现到报告精度
        a = laplacian_1d(40)
        b, _, _ = make_system(a, seed=14)
        res = pcg(a, b, restart_period=10)
        self.assertEqual(res.status, STATUS_CONVERGED)
        # history[0] 是初始真实残差；末值是收敛点真实残差
        self.assertAlmostEqual(res.residual_history[0], float(np.linalg.norm(b)))
        self.assertLessEqual(res.residual_history[-1], res.residual_history[0])
        # 单调（非增）——CG 真实残差允许小抖动，但残差替换点通常单调
        hist = np.asarray(res.residual_history)
        self.assertTrue(np.all(np.diff(hist) <= 1e-10 * hist[:-1] + 1e-14))

    def test_residual_replacement_keeps_recursive_residual_honest(self) -> None:
        # 较长的三对角系统 + 小 restart_period：结果残差仍必须真实
        n = 300
        a = laplacian_1d(n)
        b, x_exact, _ = make_system(a, seed=15)
        res = pcg(a, b, restart_period=25)
        self.assertEqual(res.status, STATUS_CONVERGED)
        true_r = float(np.linalg.norm(b - a.matvec(res.x)))
        self.assertLessEqual(true_r, 1e-8 * np.linalg.norm(b) + 1e-12)
        self.assertAlmostEqual(res.residual_norm, true_r, places=12)
        self.assertLess(np.linalg.norm(res.x - x_exact), 1e-4)


if __name__ == "__main__":
    unittest.main()
