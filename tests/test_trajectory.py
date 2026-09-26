"""合成传感器轨迹拟合测试。"""

import unittest

import numpy as np

from collision_detection import CircleBody, analyze_pair, fit_linear_trajectory


class TestFitLinearTrajectory(unittest.TestCase):
    def test_exact_recovery_without_noise(self):
        # 真值 c=(1,2), v=(3,-1)：无噪声采样必须精确还原
        t = np.linspace(0.0, 5.0, 11)
        xy = np.array([1.0, 2.0]) + np.outer(t, [3.0, -1.0])
        samples = np.column_stack([t, xy])
        body = fit_linear_trajectory(samples, radius=0.5)
        np.testing.assert_allclose(body.center, [1.0, 2.0], atol=1e-10)
        np.testing.assert_allclose(body.velocity, [3.0, -1.0], atol=1e-10)
        self.assertEqual(body.radius, 0.5)

    def test_noisy_fit_close_to_truth(self):
        # 固定种子噪声下，拟合参数应贴近真值
        rng = np.random.default_rng(7)
        t = np.linspace(0.0, 4.0, 41)
        xy = np.array([0.0, 0.0]) + np.outer(t, [1.0, 0.0])
        xy += rng.normal(0.0, 0.02, xy.shape)
        body = fit_linear_trajectory(np.column_stack([t, xy]), radius=1.0)
        np.testing.assert_allclose(body.center, [0.0, 0.0], atol=0.05)
        np.testing.assert_allclose(body.velocity, [1.0, 0.0], atol=0.05)

    def test_fitted_trajectory_collision_matches_truth(self):
        # 真值：robot c=(0,0) v=(1,0) r=1；obstacle c=(6,0.5) v=(-1,0) r=1
        # d0=(-6,-0.5), v_rel=(2,0), R=2
        # a=4, b=-24, c=36.25-4=32.25, D=576-516=60
        # t=(24±sqrt(60))/8 ≈ 2.032, 3.968 —— 拟合结果应落在真值附近
        rng = np.random.default_rng(42)
        t = np.linspace(0.0, 2.0, 21)

        def samples(center, velocity):
            xy = np.asarray(center) + np.outer(t, velocity)
            xy += rng.normal(0.0, 0.02, xy.shape)
            return np.column_stack([t, xy])

        robot = fit_linear_trajectory(samples([0, 0], [1, 0]), 1.0)
        obstacle = fit_linear_trajectory(samples([6, 0.5], [-1, 0]), 1.0)
        result = analyze_pair(robot, obstacle, 0.0, 4.0)
        self.assertTrue(result.collides)
        expected = (24 - np.sqrt(60)) / 8
        self.assertAlmostEqual(result.t_enter, expected, delta=0.05)

    def test_rejects_bad_shape(self):
        with self.assertRaises(ValueError):
            fit_linear_trajectory(np.zeros((3, 2)), 1.0)
        with self.assertRaises(ValueError):
            fit_linear_trajectory(np.zeros((1, 3)), 1.0)


if __name__ == "__main__":
    unittest.main()
