"""核心算法测试：正确性、缺测、部分观测、数值性质、非法输入。"""

from __future__ import annotations

import unittest

import numpy as np

from kfmu import (
    DimensionError,
    InvalidValueError,
    KalmanFilter,
    LinearKalmanModel,
    MatrixPropertyError,
    NumericalStabilityError,
    predict,
    run_batch,
    update,
)

DT = 1.0


def cv_1d_model(q=0.01, r=1.0):
    """一维恒速模型，状态 [位置, 速度]。"""
    F = np.array([[1.0, DT], [0.0, 1.0]])
    H = np.array([[1.0, 0.0]])
    Q = q * np.array([[DT**4 / 4, DT**3 / 2], [DT**3 / 2, DT**2]])
    R = np.array([[r]])
    return LinearKalmanModel(F=F, H=H, Q=Q, R=R)


def cv_2d_hetero(q=0.5, rx=1.0, ry_mm=400.0):
    """二维恒速模型，状态 [x, vx, y, vy]。

    量测 x 以米、y 以毫米为单位（不同量纲）：H 第二行含 1000 倍缩放，
    R 分别取 1 m^2 与 400^2 mm^2。
    """
    F = np.array([
        [1, DT, 0, 0],
        [0, 1, 0, 0],
        [0, 0, 1, DT],
        [0, 0, 0, 1],
    ], dtype=float)
    H = np.array([
        [1, 0, 0, 0],
        [0, 0, 1000.0, 0],
    ])
    qb = np.array([[DT**4 / 4, DT**3 / 2], [DT**3 / 2, DT**2]])
    Q = q * np.block([[qb, np.zeros((2, 2))], [np.zeros((2, 2)), qb]])
    R = np.diag([rx, ry_mm**2])
    return LinearKalmanModel(F=F, H=H, Q=Q, R=R)


class TestSanity(unittest.TestCase):
    def test_perfect_constant_velocity_track(self):
        """无噪声恒速轨迹上估计应收敛到真值，RMSE 远小于量测噪声水平。"""
        rng = np.random.default_rng(7)
        model = cv_1d_model(q=0.002, r=1.0)
        T = 300
        truth = np.stack([np.arange(T) * 0.5, np.full(T, 0.5)], axis=1)
        z = truth[:, 0] + rng.normal(0, 1.0, size=T)

        kf = KalmanFilter(model, x0=np.array([0.0, 0.0]), P0=np.eye(2) * 100)
        est = []
        for k in range(T):
            kf.predict()
            kf.update(np.array([z[k]]))
            est.append(kf.x.copy())
        est = np.asarray(est)
        rmse_pos = np.sqrt(np.mean((est[100:, 0] - truth[100:, 0]) ** 2))
        rmse_vel = np.sqrt(np.mean((est[100:, 1] - truth[100:, 1]) ** 2))
        # 20 个种子实测（q=0.002）：位置稳态 RMSE 最大约 0.53、速度约 0.065。
        # 阈值取实测最大之上留裕度，同时远小于量测噪声 std=1。
        self.assertLess(rmse_pos, 0.65)
        self.assertLess(rmse_vel, 0.09)

    def test_covariance_shrinks_then_grows_during_gap(self):
        model = cv_1d_model(q=0.01, r=1.0)
        kf = KalmanFilter(model, np.zeros(2), np.eye(2) * 10)
        for _ in range(20):
            kf.predict()
            kf.update(np.array([1.0]))
        p_small = np.trace(kf.P)
        for _ in range(10):
            kf.predict()
            kf.update(np.array([np.nan]))  # 缺测：纯预测
        p_gap = np.trace(kf.P)
        self.assertGreater(p_gap, p_small)


class TestMissingObservations(unittest.TestCase):
    def test_all_missing_equals_predict(self):
        """全缺测：无状态 update 不改变输入；有状态接口等价于纯预测。"""
        model = cv_1d_model()
        x = np.array([1.0, -0.5])
        P = np.array([[2.0, 0.3], [0.3, 0.5]])
        r_nan = update(model, x, P, np.array([np.nan]))
        r_none = update(model, x, P, np.array([0.0]), available=np.array([False]))
        self.assertEqual(r_nan.status, "predicted_only")
        # 无状态接口：全缺测不做任何时间更新，原样返回
        np.testing.assert_array_equal(r_nan.x, x)
        np.testing.assert_array_equal(r_nan.P, P)
        np.testing.assert_array_equal(r_none.x, x)
        np.testing.assert_array_equal(r_none.P, P)
        self.assertIsNone(r_nan.innovation)

        # 有状态接口：predict 后全缺测，结果等于预测值
        kf = KalmanFilter(model, x, P)
        xp, Pp = predict(model, x, P)
        kf.predict()
        r = kf.update(np.array([np.nan]))
        np.testing.assert_allclose(r.x, xp)
        np.testing.assert_allclose(r.P, Pp)

    def test_partial_update_matches_subspace_model(self):
        """部分分量更新必须等价于只保留观测分量的降维模型更新。"""
        rng = np.random.default_rng(11)
        n, m = 4, 3
        F = np.eye(n)
        H = rng.normal(size=(m, n))
        Q = (W := rng.normal(size=(n, n))) @ W.T * 0.01 + np.eye(n) * 0.01
        R = np.diag([1.0, 2.0, 0.5])
        model = LinearKalmanModel(F, H, Q, R)

        x = rng.normal(size=n)
        P = (A := rng.normal(size=(n, n))) @ A.T + np.eye(n)
        z = rng.normal(size=m)

        # 只使用第 0、2 个分量
        avail = np.array([True, False, True])
        r_part = update(model, x, P, z, available=avail)

        idx = np.array([0, 2])
        sub_model = LinearKalmanModel(F, H[idx], Q, R[np.ix_(idx, idx)])
        r_sub = update(sub_model, x, P, z[idx])

        np.testing.assert_allclose(r_part.x, r_sub.x, rtol=1e-10, atol=1e-12)
        np.testing.assert_allclose(r_part.P, r_sub.P, rtol=1e-10, atol=1e-12)
        np.testing.assert_allclose(r_part.innovation, r_sub.innovation)

    def test_continuous_gap_returns_to_track(self):
        """连续 30 步缺测后，重新有量测应把估计拉回轨迹。"""
        rng = np.random.default_rng(3)
        model = cv_1d_model(q=0.02, r=1.0)
        T = 120
        truth_pos = 0.3 * np.arange(T)
        z = truth_pos + rng.normal(0, 1.0, T)
        kf = KalmanFilter(model, np.zeros(2), np.eye(2) * 10)
        for k in range(T):
            kf.predict()
            if 40 <= k < 70:
                kf.update(np.array([np.nan]))
            else:
                kf.update(np.array([z[k]]))
        err = abs(kf.x[0] - truth_pos[-1])
        self.assertLess(err, 1.0)  # 重收敛
        # 缺测段中间状态：仅靠外推，误差允许更大但不发散到 NaN
        self.assertTrue(np.all(np.isfinite(kf.x)))


class TestSingularInnovation(unittest.TestCase):
    def test_zero_prior_and_zero_R_raises_numeric_error(self):
        """P=0 且 R=0 => S=0 奇异：必须抛出带状态码的异常而非崩溃/NaN。"""
        model = LinearKalmanModel(
            F=np.eye(2),
            H=np.array([[1.0, 0.0]]),
            Q=np.zeros((2, 2)),
            R=np.zeros((1, 1)),
        )
        kf = KalmanFilter(model, np.zeros(2), np.zeros((2, 2)))
        kf.predict()
        with self.assertRaises(NumericalStabilityError) as cm:
            kf.update(np.array([1.0]))
        self.assertEqual(cm.exception.code, "singular_innovation_covariance")
        self.assertTrue(np.all(np.isfinite(kf.x)))

    def test_singular_full_R_ok_for_independent_components(self):
        """R 奇异（一个分量无噪声）但 S 满秩时更新应正常。"""
        model = LinearKalmanModel(
            F=np.eye(2),
            H=np.eye(2),
            Q=np.eye(2) * 0.01,
            R=np.diag([0.0, 1.0]),
        )
        r = update(model, np.zeros(2), np.eye(2), np.array([3.0, 0.0]))
        self.assertEqual(r.status, "updated")
        self.assertAlmostEqual(r.x[0], 3.0, places=6)

    def test_batch_singular_step_is_recorded_and_continues(self):
        model = LinearKalmanModel(
            F=np.eye(1), H=np.eye(1), Q=np.zeros((1, 1)), R=np.zeros((1, 1))
        )
        zs = [np.array([1.0]), np.array([2.0])]
        res = run_batch(model, np.zeros(1), np.zeros((1, 1)), zs)
        self.assertEqual(len(res.records), 2)
        self.assertTrue(all(rec.status == "error" for rec in res.records))
        self.assertEqual(res.records[0].error_code, "singular_innovation_covariance")
        self.assertFalse(res.ok)


class TestCovarianceProperties(unittest.TestCase):
    def test_symmetry_and_psd_across_random_runs(self):
        rng = np.random.default_rng(42)
        trial = 0
        attempts = 0
        while trial < 25 and attempts < 200:
            attempts += 1
            n, m = 1 + trial % 4, 1 + trial % 3
            F = np.eye(n) + rng.normal(0, 0.05, (n, n))
            H = rng.normal(size=(m, n))
            Q = (W := rng.normal(size=(n, n))) @ W.T * 0.1 + np.eye(n) * 1e-6
            R = (V := rng.normal(size=(m, m))) @ V.T * 0.1 + np.eye(m) * 1e-4
            try:
                model = LinearKalmanModel(F, H, Q, R)
            except MatrixPropertyError:
                continue
            trial += 1
            kf = KalmanFilter(model, rng.normal(size=n), np.eye(n) * 5)
            for k in range(10):
                kf.predict()
                avail = rng.random(m) > 0.4
                z = rng.normal(size=m)
                if avail.any():
                    kf.update(z, available=avail)
                P = kf.P
                np.testing.assert_allclose(P, P.T, atol=1e-9)
                self.assertGreaterEqual(
                    np.linalg.eigvalsh(P)[0], -1e-8,
                    msg=f"trial {trial} step {k} P 非 PSD"
                )
        self.assertEqual(trial, 25)

    def test_heterogeneous_units_stay_psd_and_track(self):
        """不同量纲（米 / 毫米）下：协方差保持 PSD，两轴都能跟踪。"""
        rng = np.random.default_rng(99)
        model = cv_2d_hetero()
        T = 150
        # 真值单位：米；y 量测换算成毫米
        truth = np.zeros((T, 4))
        for k in range(T):
            truth[k] = [2.0 * k, 2.0, -1.5 * k, -1.5]
        z = np.column_stack([
            truth[:, 0] + rng.normal(0, 1.0, T),
            truth[:, 2] * 1000.0 + rng.normal(0, 400.0, T),
        ])
        kf = KalmanFilter(model, np.zeros(4), np.eye(4) * 100)
        for k in range(T):
            kf.predict()
            kf.update(z[k])
            np.testing.assert_allclose(kf.P, kf.P.T, atol=1e-9)
            self.assertGreater(np.linalg.eigvalsh(kf.P)[0], -1e-8)
        # 毫米量测的轴同样收敛（换算回米比较）
        self.assertAlmostEqual(kf.x[0], truth[-1, 0], delta=2.0)
        self.assertAlmostEqual(kf.x[2], truth[-1, 2], delta=2.0)
        self.assertAlmostEqual(kf.x[1], 2.0, delta=0.5)
        self.assertAlmostEqual(kf.x[3], -1.5, delta=0.5)

    def test_hetero_with_missing_y_axis(self):
        """只缺 y（毫米轴）的连续缺测：x 轴继续被观测、保持精度。"""
        rng = np.random.default_rng(5)
        model = cv_2d_hetero()
        kf = KalmanFilter(model, np.zeros(4), np.eye(4) * 50)
        for k in range(60):
            kf.predict()
            z = np.array([2.0 * k + rng.normal(0, 1.0), np.nan])
            r = kf.update(z)
        self.assertTrue(np.all(r.available == [True, False]))
        self.assertAlmostEqual(kf.x[0], 2.0 * 59, delta=2.0)
        # y 位置从未被观测，后验方差应仍很大
        self.assertGreater(kf.P[2, 2], 10.0)


class TestValidation(unittest.TestCase):
    def test_dimension_mismatch(self):
        model = cv_1d_model()
        with self.assertRaises(DimensionError):
            KalmanFilter(model, np.zeros(3), np.eye(2))  # x0 维数错
        with self.assertRaises(DimensionError):
            update(model, np.zeros(2), np.eye(2), np.zeros(2))  # z 维数错

    def test_bad_F_shape(self):
        with self.assertRaises(DimensionError):
            LinearKalmanModel(np.zeros((2, 3)), np.zeros((1, 2)),
                              np.eye(2), np.eye(1))

    def test_Q_not_symmetric(self):
        F = np.eye(2)
        H = np.array([[1.0, 0.0]])
        Q = np.array([[1.0, 0.5], [0.2, 1.0]])  # 不对称
        with self.assertRaises(MatrixPropertyError):
            LinearKalmanModel(F, H, Q, np.eye(1))

    def test_R_negative_definite(self):
        with self.assertRaises(MatrixPropertyError):
            LinearKalmanModel(np.eye(2), np.array([[1.0, 0.0]]),
                              np.eye(2), np.array([[-1.0]]))

    def test_nan_and_inf_rejected(self):
        model = cv_1d_model()
        kf = KalmanFilter(model, np.zeros(2), np.eye(2))
        kf.predict()
        with self.assertRaises(InvalidValueError):
            kf.update(np.array([np.inf]))
        with self.assertRaises(InvalidValueError):
            LinearKalmanModel(np.eye(2), np.array([[1.0, 0.0]]),
                              np.full((2, 2), np.nan), np.eye(1))

    def test_value_range_limit(self):
        with self.assertRaises(InvalidValueError):
            LinearKalmanModel(np.eye(2) * 1e13, np.array([[1.0, 0.0]]),
                              np.eye(2), np.eye(1))

    def test_control_input_validation(self):
        model = LinearKalmanModel(
            np.eye(2), np.array([[1.0, 0.0]]), np.eye(2) * 0.01, np.eye(1),
            B=np.array([[0.0], [1.0]]),
        )
        kf = KalmanFilter(model, np.zeros(2), np.eye(2))
        with self.assertRaises(DimensionError):
            kf.predict(u=np.array([1.0, 2.0]))
        kf.predict(u=np.array([1.0]))
        self.assertAlmostEqual(kf.x[1], 1.0)


if __name__ == "__main__":
    unittest.main()
