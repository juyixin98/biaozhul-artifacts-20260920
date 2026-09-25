"""验收场景：合成二维恒速轨迹。

覆盖验收要求：
  1. 连续缺测（15 步全缺测、只缺 y 轴）；
  2. 奇异创新协方差（注入 Q=R=P=0 的一步，必须记录错误码并继续）；
  3. 不同量纲（x 量测单位米，y 量测单位毫米）；
  4. 每步检查协方差对称性与半正定容差；
  5. 经 JSON 接口端到端运行（process_request）。
"""

from __future__ import annotations

import unittest

import numpy as np

from kfmu import KalmanFilter, LinearKalmanModel, run_batch
from kfmu.jsonio import process_request

DT = 1.0
T = 160
GAP_START, GAP_END = 60, 75          # 连续全缺测区间
VEL = np.array([2.0, -1.5])           # 真实速度（米/秒）
SIGMA_X_M = 1.0                       # x 量测噪声 1 m
SIGMA_Y_MM = 400.0                    # y 量测噪声 400 mm


def build_system(q: float = 0.005):
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
    R = np.diag([SIGMA_X_M**2, SIGMA_Y_MM**2])
    return LinearKalmanModel(F=F, H=H, Q=Q, R=R)


def generate_truth_and_measurements(seed=20260923):
    rng = np.random.default_rng(seed)
    truth = np.zeros((T, 4))
    for k in range(T):
        truth[k] = [VEL[0] * k, VEL[0], VEL[1] * k, VEL[1]]
    z = np.column_stack([
        truth[:, 0] + rng.normal(0, SIGMA_X_M, T),
        truth[:, 2] * 1000.0 + rng.normal(0, SIGMA_Y_MM, T),
    ])
    return truth, z


def availability_schedule():
    """返回每步布尔掩码：正常 / 全缺测段 / 仅缺 y 段。"""
    masks = np.ones((T, 2), dtype=bool)
    masks[GAP_START:GAP_END, :] = False          # 连续全缺测
    masks[GAP_END:GAP_END + 15, 1] = False       # 仅缺 y（毫米轴）
    return masks


class TestAcceptanceConstantVelocity(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.model = build_system()
        cls.truth, cls.z = generate_truth_and_measurements()
        cls.masks = availability_schedule()

    def test_01_run_and_tracking_accuracy(self):
        kf = KalmanFilter(self.model, np.zeros(4), np.eye(4) * 100.0)
        for k in range(T):
            kf.predict()
            kf.update(self.z[k], available=self.masks[k])
        # 末段（重新获得量测后）跟踪误差
        final_err = np.abs(kf.x[[0, 2]] - self.truth[-1, [0, 2]])
        np.testing.assert_array_less(final_err, [3.0, 3.0])
        np.testing.assert_allclose(kf.x[[1, 3]], VEL, atol=0.4)

    def test_02_gap_behavior_is_pure_prediction(self):
        """缺测段：协方差随时间增长，状态等于恒速外推。"""
        kf = KalmanFilter(self.model, np.zeros(4), np.eye(4) * 100.0)
        traces = []
        for k in range(T):
            x_before = kf.x.copy()
            P_before = kf.P.copy()
            kf.predict()
            x_pred = kf.x.copy()
            r = kf.update(self.z[k], available=self.masks[k])
            if GAP_START <= k < GAP_END:
                self.assertEqual(r.status, "predicted_only")
                # 纯预测：后验 == 先验 == F x_before
                np.testing.assert_allclose(kf.x, self.model.F @ x_before)
                np.testing.assert_allclose(kf.x, x_pred)
                np.testing.assert_allclose(
                    kf.P, self.model.F @ P_before @ self.model.F.T + self.model.Q
                )
            traces.append(np.trace(kf.P))
        # 缺测段不确定度应单调增长
        gap_traces = traces[GAP_START:GAP_END]
        self.assertTrue(all(b > a for a, b in zip(gap_traces, gap_traces[1:])))

    def test_03_singular_innovation_recorded_and_resumes(self):
        """奇异创新协方差（P=Q=R=0）：记录错误码、保留先验、运行不中断。

        退化模型 Q=0、R=0、P0=0 时 S=HPH'+R=0 必然奇异。滤波器对每一步
        都应抛出/记录 ``singular_innovation_covariance``，且批量运行能够
        走完整个序列，后续步仍给出有限的预测状态。
        """
        degenerate = LinearKalmanModel(
            F=self.model.F, H=self.model.H,
            Q=np.zeros((4, 4)),
            R=np.zeros((2, 2)),
        )
        zs = [self.z[k] for k in range(T)]
        res = run_batch(
            degenerate, np.zeros(4), np.zeros((4, 4)),
            measurements=zs, masks=list(self.masks),
        )

        # 全缺测段为纯预测（无 S），其余有量测步全部应报奇异
        err_steps = [r for r in res.records if r.status == "error"]
        pred_only = [r for r in res.records if r.status == "predicted_only"]
        self.assertEqual(len(pred_only), GAP_END - GAP_START)
        self.assertTrue(len(err_steps) >= 1)
        self.assertTrue(all(
            r.error_code == "singular_innovation_covariance" for r in err_steps
        ))
        # 出错步保留的先验必须有限
        for r in err_steps:
            self.assertTrue(np.all(np.isfinite(r.x)))
        # 运行未中断，走完 T 步
        self.assertEqual(res.records[-1].index, T - 1)

        # 无状态接口同样抛出带状态码的异常，而不是产生 NaN
        from kfmu import update
        with self.assertRaises(Exception) as cm:
            update(degenerate, np.zeros(4), np.zeros((4, 4)), self.z[0])
        self.assertEqual(cm.exception.code, "singular_innovation_covariance")

    def test_04_symmetry_psd_every_step(self):
        kf = KalmanFilter(self.model, np.zeros(4), np.eye(4) * 100.0)
        SYM_TOL = 1e-9
        PSD_TOL = 1e-8
        for k in range(T):
            kf.predict()
            kf.update(self.z[k], available=self.masks[k])
            P = kf.P
            asym = np.max(np.abs(P - P.T))
            eig_min = np.linalg.eigvalsh(0.5 * (P + P.T))[0]
            self.assertLessEqual(
                float(asym), SYM_TOL, msg=f"step {k} 不对称：{asym}"
            )
            self.assertGreaterEqual(
                float(eig_min), -PSD_TOL, msg=f"step {k} 非 PSD：{eig_min}"
            )

    def test_05_end_to_end_json_with_all_features(self):
        """通过 JSON 接口跑完整场景：缺测用 null，奇异步记录错误。"""
        measurements = []
        for k in range(T):
            if not self.masks[k].any():
                measurements.append(None)
            else:
                measurements.append([
                    None if not self.masks[k, 0] else float(self.z[k, 0]),
                    None if not self.masks[k, 1] else float(self.z[k, 1]),
                ])
        payload = {
            "model": {
                "F": self.model.F.tolist(),
                "H": self.model.H.tolist(),
                "Q": self.model.Q.tolist(),
                "R": self.model.R.tolist(),
            },
            "initial_state": {
                "x": [0.0, 0.0, 0.0, 0.0],
                "P": (np.eye(4) * 100.0).tolist(),
            },
            "measurements": measurements,
        }
        resp = process_request(payload)
        self.assertEqual(resp["summary"]["num_steps"], T)
        self.assertEqual(
            resp["summary"]["num_predicted_only"], GAP_END - GAP_START
        )
        self.assertTrue(resp["ok"])

        # 仅缺 y 的步：仍为 updated，且 available=[true,false]
        partial = resp["steps"][GAP_END]
        self.assertEqual(partial["status"], "updated")
        self.assertEqual(partial["available"], [True, False])

        # 全部步的对称性/PSD 诊断都在容差内
        for step in resp["steps"]:
            if step["status"] != "error":
                self.assertGreaterEqual(
                    step["diagnostics"]["min_eigenvalue"], -1e-8
                )
                self.assertLessEqual(
                    step["diagnostics"]["max_asymmetry"], 1e-9
                )

        # 末态精度（经 JSON 往返后仍是普通 float）
        fx = resp["final"]["x"]
        self.assertAlmostEqual(fx[0], self.truth[-1, 0], delta=3.0)
        self.assertAlmostEqual(fx[2], self.truth[-1, 2], delta=3.0)

        # 注入奇异步再跑一次：降级模型 + 单点 P=0 起始
        singular_payload = {
            "model": {
                "F": self.model.F.tolist(),
                "H": self.model.H.tolist(),
                "Q": np.zeros((4, 4)).tolist(),
                "R": np.zeros((2, 2)).tolist(),
            },
            "initial_state": {"x": [0.0] * 4, "P": np.zeros((4, 4)).tolist()},
            "measurements": [[float(self.z[0, 0]), float(self.z[0, 1])]],
        }
        resp2 = process_request(singular_payload)
        self.assertFalse(resp2["ok"])
        self.assertEqual(
            resp2["steps"][0]["error"]["code"],
            "singular_innovation_covariance",
        )


if __name__ == "__main__":
    unittest.main()
