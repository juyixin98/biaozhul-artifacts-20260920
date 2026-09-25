"""手算数据核对（验收项 1）。

所有期望值均由纸笔推导，不调用被测代码反推：

数据集 A（含端点，且端点上预测正确）::

    y = [0, 1, 1, 0], p = [0.0, 0.5, 1.0, 0.5]

    Brier = ((0-0)^2 + 2*(0.5-1)^2 + (0.5-0)^2 + (0-0)^2) / 4
          = (0 + 0.25 + 0 + 0.25) / 4 = 0.125

    对数损失（端点上预测正确，-ln(1)=0，不发散）:
        LL = (0 + ln2 + 0 + ln2) / 4 = ln2/2

数据集 B（用于 ECE 手算，n_bins=2）::

    y = [0, 1], p = [0.25, 0.75]
    箱 [0,.5):  acc=0,   conf=.25, gap=.25, 权重 1
    箱 [.5,1]:  acc=1,   conf=.75, gap=.25, 权重 1
    ECE = .5*.25 + .5*.25 = .25
    Brier = (.25^2 + .25^2)/2 = .0625
    LL = ln(4/3)

数据集 C（全同概率，p 全部为 .25，n_bins=4）::

    y = [0,0,1,1], p = [.25]*4
    全部落入箱 1 [.25,.5): acc=.5, conf=.25, gap=.25
    ECE = .25
    Brier = (2*.25^2 + 2*.75^2)/4 = (0.125+1.125)/4 = .3125
    LL = -(2*ln(.75) + 2*ln(.25))/4
"""

from __future__ import annotations

import math
import unittest

from calibration.metrics import (
    brier_score,
    expected_calibration_error,
    log_loss,
)


class HandVerifiedMetricsTest(unittest.TestCase):
    """数据集 A/B/C 的指标必须与手算值逐位吻合。"""

    def test_dataset_a_brier(self):
        y = [0, 1, 1, 0]
        p = [0.0, 0.5, 1.0, 0.5]
        self.assertAlmostEqual(brier_score(y, p), 0.125, places=15)

    def test_dataset_a_log_loss_endpoints_correct(self):
        y = [0, 1, 1, 0]
        p = [0.0, 0.5, 1.0, 0.5]
        expected = math.log(2) / 2
        # 三种端点策略在"端点预测正确"时结果一致。
        self.assertAlmostEqual(log_loss(y, p, endpoint_strategy="clip"), expected)
        self.assertAlmostEqual(log_loss(y, p, endpoint_strategy="error"), expected)
        self.assertAlmostEqual(log_loss(y, p, endpoint_strategy="ignore"), expected)

    def test_dataset_b_ece_two_bins(self):
        y = [0, 1]
        p = [0.25, 0.75]
        self.assertAlmostEqual(
            expected_calibration_error(y, p, n_bins=2), 0.25, places=15
        )

    def test_dataset_b_brier_and_logloss(self):
        y = [0, 1]
        p = [0.25, 0.75]
        self.assertAlmostEqual(brier_score(y, p), 0.0625, places=15)
        self.assertAlmostEqual(log_loss(y, p), math.log(4 / 3), places=15)

    def test_dataset_c_identical_probabilities(self):
        y = [0, 0, 1, 1]
        p = [0.25, 0.25, 0.25, 0.25]
        self.assertAlmostEqual(
            expected_calibration_error(y, p, n_bins=4), 0.25, places=15
        )
        self.assertAlmostEqual(brier_score(y, p), 0.3125, places=15)
        expected_ll = -(2 * math.log(0.75) + 2 * math.log(0.25)) / 4
        self.assertAlmostEqual(log_loss(y, p), expected_ll, places=15)

    def test_perfect_prediction_is_zero(self):
        y = [0, 1, 0, 1]
        p = [0.0, 1.0, 0.0, 1.0]
        self.assertEqual(brier_score(y, p), 0.0)
        self.assertEqual(log_loss(y, p, endpoint_strategy="error"), 0.0)
        self.assertEqual(
            expected_calibration_error(y, p, n_bins=3), 0.0
        )


if __name__ == "__main__":
    unittest.main()
