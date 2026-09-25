"""边界场景与非法输入测试（验收项 3）。

覆盖：
- 全同概率（含全 0、全 0.5、全 1）；
- 极端类别不均（一万比一）；
- 非法概率（<0、>1、NaN、Inf）；
- 非法标签、长度不一致、空输入、非法权重；
- 三种概率端点策略的行为差异；
- ECE 分箱细节（空箱、p=1.0 落箱、n_bins=1）。
"""

from __future__ import annotations

import math
import unittest

import numpy as np

from calibration.errors import CalibrationError
from calibration.metrics import (
    brier_score,
    expected_calibration_error,
    log_loss,
)


class IdenticalProbabilitiesTest(unittest.TestCase):
    def test_all_probability_half_balanced_labels(self):
        # p≡0.5：Brier 与 ECE 都与标签中正类比例 q 的 |.5-q| 相关。
        y = [0, 0, 1, 1]
        p = [0.5] * 4
        self.assertAlmostEqual(brier_score(y, p), 0.25, places=15)
        self.assertAlmostEqual(
            expected_calibration_error(y, p, n_bins=2), 0.0, places=15
        )
        # acc=.5、conf=.5，所以 gap 为 0（尽管 Brier 不为 0）。
        self.assertAlmostEqual(log_loss(y, p), math.log(2), places=15)

    def test_all_probability_zero_all_negative(self):
        y = [0, 0, 0]
        p = [0.0, 0.0, 0.0]
        self.assertEqual(brier_score(y, p), 0.0)
        self.assertEqual(log_loss(y, p, endpoint_strategy="error"), 0.0)
        self.assertEqual(expected_calibration_error(y, p, n_bins=5), 0.0)

    def test_all_probability_one_all_positive(self):
        y = [1, 1, 1]
        p = [1.0, 1.0, 1.0]
        self.assertEqual(brier_score(y, p), 0.0)
        self.assertEqual(log_loss(y, p, endpoint_strategy="error"), 0.0)
        self.assertEqual(expected_calibration_error(y, p, n_bins=5), 0.0)

    def test_all_probability_extreme_wrong_label_clip(self):
        # 全 1 概率但全是负标签：error 策略发散，clip 给出有限大损失。
        y = [0, 0]
        p = [1.0, 1.0]
        clipped = log_loss(y, p, endpoint_strategy="clip", epsilon=1e-12)
        self.assertTrue(math.isfinite(clipped))
        self.assertAlmostEqual(clipped, -math.log(1e-12), places=8)

    def test_all_same_interior_probability_single_bin(self):
        y = [1, 0, 0, 0, 0]
        p = [0.2] * 5
        # 单箱 ECE = |acc - conf| = |.2 - .2| = 0
        self.assertEqual(
            expected_calibration_error(y, p, n_bins=1), 0.0
        )


class ExtremeImbalanceTest(unittest.TestCase):
    def test_ten_thousand_to_one_imbalance(self):
        rng = np.random.default_rng(7)
        n_neg, n_pos = 10000, 1
        y = np.r_[np.zeros(n_neg), np.ones(n_pos)]
        # 模型对负样本给很小概率，对唯一正样本给中等概率。
        p = np.r_[
            np.full(n_neg, 0.0001),
            np.array([0.6]),
        ]
        bs = brier_score(y, p)
        ll = log_loss(y, p)
        ece = expected_calibration_error(y, p, n_bins=10)
        # 手算：10000 个 (0-.0001)^2 + 1 个 (.6-1)^2，除以 10001。
        expected_bs = (n_neg * 1e-8 + 0.16) / (n_neg + n_pos)
        self.assertAlmostEqual(bs, expected_bs, places=15)
        self.assertTrue(math.isfinite(ll) and ll >= 0.0)
        self.assertTrue(0.0 <= ece <= 1.0)

    def test_rare_positive_endpoint_zero(self):
        # 极端不均 + 唯一正样本概率恰好为 0：error 必须报错。
        y = np.r_[np.zeros(100), 1.0]
        p = np.r_[np.full(100, 0.1), 0.0]
        with self.assertRaises(CalibrationError) as ctx:
            log_loss(y, p, endpoint_strategy="error")
        self.assertEqual(ctx.exception.code, "ENDPOINT_LOSS")
        # ignore 策略跳过该样本，仅评估其余 100 个。
        ll = log_loss(y, p, endpoint_strategy="ignore")
        self.assertAlmostEqual(ll, -math.log(0.9), places=15)


class InvalidInputTest(unittest.TestCase):
    def test_probability_above_one_rejected(self):
        with self.assertRaises(CalibrationError) as ctx:
            brier_score([0, 1], [0.5, 1.0001])
        self.assertEqual(ctx.exception.code, "INVALID_PROBABILITY")

    def test_probability_below_zero_rejected(self):
        with self.assertRaises(CalibrationError) as ctx:
            brier_score([0, 1], [-0.0001, 0.5])
        self.assertEqual(ctx.exception.code, "INVALID_PROBABILITY")

    def test_nan_probability_rejected(self):
        with self.assertRaises(CalibrationError) as ctx:
            brier_score([0, 1], [0.5, float("nan")])
        self.assertEqual(ctx.exception.code, "INVALID_INPUT")

    def test_inf_probability_rejected(self):
        with self.assertRaises(CalibrationError) as ctx:
            brier_score([0, 1], [0.5, float("inf")])
        self.assertEqual(ctx.exception.code, "INVALID_INPUT")

    def test_invalid_label_rejected(self):
        with self.assertRaises(CalibrationError) as ctx:
            brier_score([0, 2], [0.5, 0.5])
        self.assertEqual(ctx.exception.code, "INVALID_LABEL")

    def test_length_mismatch_rejected(self):
        with self.assertRaises(CalibrationError) as ctx:
            brier_score([0, 1, 0], [0.5, 0.5])
        self.assertEqual(ctx.exception.code, "LENGTH_MISMATCH")

    def test_empty_input_rejected(self):
        with self.assertRaises(CalibrationError) as ctx:
            brier_score([], [])
        self.assertEqual(ctx.exception.code, "EMPTY_INPUT")

    def test_negative_weight_rejected(self):
        with self.assertRaises(CalibrationError) as ctx:
            brier_score([0, 1], [0.5, 0.5], sample_weight=[1, -1])
        self.assertEqual(ctx.exception.code, "INVALID_WEIGHT")

    def test_all_zero_weight_rejected(self):
        with self.assertRaises(CalibrationError) as ctx:
            brier_score([0, 1], [0.5, 0.5], sample_weight=[0, 0])
        self.assertEqual(ctx.exception.code, "INVALID_WEIGHT")

    def test_weight_length_mismatch_rejected(self):
        with self.assertRaises(CalibrationError) as ctx:
            brier_score([0, 1], [0.5, 0.5], sample_weight=[1])
        self.assertEqual(ctx.exception.code, "LENGTH_MISMATCH")

    def test_invalid_n_bins_rejected(self):
        for bad in (0, -3, 2.5, True, "10"):
            with self.subTest(bad=bad):
                with self.assertRaises(CalibrationError) as ctx:
                    expected_calibration_error(
                        [0, 1], [0.2, 0.8], n_bins=bad
                    )
                self.assertEqual(ctx.exception.code, "INVALID_N_BINS")

    def test_invalid_endpoint_strategy_rejected(self):
        with self.assertRaises(CalibrationError) as ctx:
            log_loss([0, 1], [0.2, 0.8], endpoint_strategy="nope")
        self.assertEqual(ctx.exception.code, "INVALID_STRATEGY")

    def test_invalid_epsilon_rejected(self):
        for bad in (-1e-9, 0.5, 0.9, float("nan")):
            with self.subTest(bad=bad):
                with self.assertRaises(CalibrationError):
                    log_loss([0, 1], [0.2, 0.8], epsilon=bad)


class EndpointStrategyTest(unittest.TestCase):
    def test_error_strategy_raises_on_divergence(self):
        # 正标签 + p=0
        with self.assertRaises(CalibrationError) as ctx:
            log_loss([1], [0.0], endpoint_strategy="error")
        self.assertEqual(ctx.exception.code, "ENDPOINT_LOSS")
        # 负标签 + p=1
        with self.assertRaises(CalibrationError):
            log_loss([0], [1.0], endpoint_strategy="error")

    def test_ignore_skips_only_divergent_samples(self):
        # 第 0 个（y=1,p=0）发散被跳过；其余两个的损失均为 -ln(.8)：
        # y=1,p=.8 -> -ln(.8)；y=0,p=.2 -> -ln(1-.2) = -ln(.8)。
        y = [1, 1, 0]
        p = [0.0, 0.8, 0.2]
        ll = log_loss(y, p, endpoint_strategy="ignore")
        self.assertAlmostEqual(ll, -math.log(0.8), places=15)

    def test_clip_equals_manual_clipping(self):
        y = [1, 0]
        p = [0.0, 1.0]
        eps = 1e-10
        ll = log_loss(y, p, endpoint_strategy="clip", epsilon=eps)
        self.assertAlmostEqual(ll, -math.log(eps), places=10)

    def test_clip_epsilon_zero_yields_inf_without_warning(self):
        # eps=0 表示不抬升：发散项得到 inf（NumPy 语义），且不应抛告警。
        import warnings

        with warnings.catch_warnings():
            warnings.simplefilter("error", RuntimeWarning)
            divergent = log_loss(
                [1, 0], [0.0, 1.0], endpoint_strategy="clip", epsilon=0.0
            )
            correct = log_loss(
                [0, 1], [0.0, 1.0], endpoint_strategy="clip", epsilon=0.0
            )
        self.assertEqual(divergent, math.inf)
        self.assertEqual(correct, 0.0)

    def test_ignore_with_weights_skips_divergent_weight(self):
        # 发散样本即使带权也被跳过，归一化只含非发散样本。
        y = [1, 0]
        p = [0.0, 0.25]
        w = [5.0, 2.0]
        ll = log_loss(y, p, sample_weight=w, endpoint_strategy="ignore")
        self.assertAlmostEqual(
            ll, (2.0 * -math.log(0.75)) / 2.0, places=15
        )

    def test_ignore_all_divergent_rejected(self):
        with self.assertRaises(CalibrationError) as ctx:
            log_loss([1, 0], [0.0, 1.0], endpoint_strategy="ignore")
        self.assertEqual(ctx.exception.code, "ENDPOINT_LOSS")


class EceBinningTest(unittest.TestCase):
    def test_probability_one_goes_to_last_bin(self):
        y = [1]
        p = [1.0]
        ece, bins = expected_calibration_error(
            y, p, n_bins=3, return_bins=True
        )
        self.assertEqual(ece, 0.0)
        self.assertEqual(bins[-1]["count"], 1)
        self.assertTrue(all(b["count"] == 0 for b in bins[:-1]))

    def test_empty_bins_excluded_and_marked(self):
        # 两个样本只占据第 0、4 箱，其余为空。
        y = [0, 1]
        p = [0.05, 0.95]
        ece, bins = expected_calibration_error(
            y, p, n_bins=5, return_bins=True
        )
        empty = [b for b in bins if b["count"] == 0]
        nonempty = [b for b in bins if b["count"] > 0]
        self.assertEqual(len(empty), 3)
        self.assertIsNone(empty[0]["mean_proba"])
        # 两个非空箱 gap 均为 .05（conf .05 vs acc 0；conf .95 vs acc 1），
        # ECE = .5*.05 + .5*.05 = .05；3 个空箱不参与归一化。
        self.assertAlmostEqual(ece, 0.05, places=15)
        self.assertEqual(
            sum(b["weight"] for b in nonempty),
            sum(b["weight"] for b in bins),
        )

    def test_bin_boundaries_half_open(self):
        # p=0.5 在 n_bins=2 时落入箱 1 [0.5, 1]。
        _, bins = expected_calibration_error(
            [1], [0.5], n_bins=2, return_bins=True
        )
        self.assertEqual(bins[1]["count"], 1)
        self.assertEqual(bins[0]["count"], 0)

    def test_single_bin_ece_is_weighted_gap(self):
        y = [1, 1, 0, 0]
        p = [0.9, 0.7, 0.8, 0.6]
        # conf=.75, acc=.5 -> ECE=.25
        self.assertAlmostEqual(
            expected_calibration_error(y, p, n_bins=1), 0.25, places=15
        )

    def test_bin_ranges_cover_unit_interval(self):
        _, bins = expected_calibration_error(
            [0, 1], [0.1, 0.9], n_bins=4, return_bins=True
        )
        self.assertAlmostEqual(bins[0]["range"][0], 0.0)
        self.assertAlmostEqual(bins[-1]["range"][1], 1.0)
        for b in bins:
            self.assertLess(b["range"][0], b["range"][1])


if __name__ == "__main__":
    unittest.main()
