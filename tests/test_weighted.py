"""加权复制等价测试（验收项 2）。

核心不变量：把样本 i 复制 k_i 份（不带权）得到的指标，必须与
用权重 w_i = k_i 在原样本上算出的指标完全相等。整数复制与分数
权重两种情形都测；同时验证权重整体缩放不改变结果（尺度不变性）。
"""

from __future__ import annotations

import unittest

import numpy as np

from calibration.metrics import (
    brier_score,
    evaluate_calibration,
    expected_calibration_error,
    log_loss,
)


def replicate(y, p, counts):
    """按 counts 复制样本，返回不带权的大数组。"""
    y, p, counts = map(np.asarray, (y, p, counts))
    y_rep = np.repeat(y, counts)
    p_rep = np.repeat(p, counts)
    return y_rep, p_rep


class WeightedReplicationTest(unittest.TestCase):
    def setUp(self):
        self.rng = np.random.default_rng(123)
        self.y = self.rng.integers(0, 2, size=20).astype(float)
        self.p = np.round(self.rng.random(20), 3)
        self.counts = self.rng.integers(1, 6, size=20)

    def test_brier_integer_replication_equivalence(self):
        y_rep, p_rep = replicate(self.y, self.p, self.counts)
        unweighted = brier_score(y_rep, p_rep)
        weighted = brier_score(self.y, self.p, sample_weight=self.counts)
        self.assertAlmostEqual(unweighted, weighted, places=15)

    def test_logloss_integer_replication_equivalence(self):
        y_rep, p_rep = replicate(self.y, self.p, self.counts)
        unweighted = log_loss(y_rep, p_rep)
        weighted = log_loss(self.y, self.p, sample_weight=self.counts)
        self.assertAlmostEqual(unweighted, weighted, places=15)

    def test_ece_integer_replication_equivalence(self):
        y_rep, p_rep = replicate(self.y, self.p, self.counts)
        for n_bins in (1, 5, 10):
            with self.subTest(n_bins=n_bins):
                unweighted = expected_calibration_error(
                    y_rep, p_rep, n_bins=n_bins
                )
                weighted = expected_calibration_error(
                    self.y, self.p, sample_weight=self.counts, n_bins=n_bins
                )
                self.assertAlmostEqual(unweighted, weighted, places=15)

    def test_fractional_weights_match_lifted_integer_replication(self):
        # 分数权重 0.5*counts：放大 2 倍成整数复制后，指标必须相同。
        frac_weights = self.counts.astype(float) / 2.0
        lifted_counts = self.counts * 2
        y_rep, p_rep = replicate(self.y, self.p, lifted_counts)
        self.assertAlmostEqual(
            brier_score(y_rep, p_rep),
            brier_score(self.y, self.p, sample_weight=frac_weights),
            places=15,
        )
        self.assertAlmostEqual(
            log_loss(y_rep, p_rep),
            log_loss(self.y, self.p, sample_weight=frac_weights),
            places=15,
        )
        self.assertAlmostEqual(
            expected_calibration_error(y_rep, p_rep, n_bins=7),
            expected_calibration_error(
                self.y, self.p, sample_weight=frac_weights, n_bins=7
            ),
            places=15,
        )

    def test_weight_scale_invariance(self):
        scale = 3.7
        for metric_name, fn in (
            ("brier", brier_score),
            ("logloss", log_loss),
            ("ece", expected_calibration_error),
        ):
            with self.subTest(metric=metric_name):
                self.assertAlmostEqual(
                    fn(self.y, self.p),
                    fn(self.y, self.p, sample_weight=np.full(20, scale)),
                    places=15,
                )

    def test_evaluate_reports_total_weight(self):
        result = evaluate_calibration(
            self.y, self.p, sample_weight=self.counts.astype(float)
        )
        self.assertEqual(
            result["total_weight"], float(np.asarray(self.counts).sum())
        )

    def test_zero_weight_samples_have_no_effect(self):
        # 零权重样本等于不存在：追加任意零权重样本不改变任何指标。
        y = np.array([1.0, 0.0, 1.0])
        p = np.array([0.9, 0.1, 0.8])
        w = np.array([1.0, 1.0, 1.0])
        y2 = np.r_[y, 0.0, 1.0]
        p2 = np.r_[p, 0.99, 0.01]
        w2 = np.r_[w, 0.0, 0.0]
        self.assertAlmostEqual(
            brier_score(y, p, w), brier_score(y2, p2, w2), places=15
        )
        self.assertAlmostEqual(
            log_loss(y, p, w), log_loss(y2, p2, w2), places=15
        )
        self.assertAlmostEqual(
            expected_calibration_error(y, p, w),
            expected_calibration_error(y2, p2, w2),
            places=15,
        )


if __name__ == "__main__":
    unittest.main()
