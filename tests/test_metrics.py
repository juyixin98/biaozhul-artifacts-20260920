"""指标正确性测试:手算核对、加权复制等价、边界场景、非法输入。"""

import math

import numpy as np
import pytest

from calibration_eval.metrics import (
    brier_score,
    calibration_bins,
    evaluate,
    expected_calibration_error,
    log_loss,
)

# ---------------------------------------------------------------- 手算核对数据
# y = [1, 0, 1, 0], p = [0.9, 0.2, 0.6, 0.4]
# Brier = (0.01 + 0.04 + 0.16 + 0.16) / 4 = 0.0925
# LogLoss = -(ln0.9 + ln0.8 + ln0.6 + ln0.6) / 4
# ECE (n_bins=2): 箱0 {p=0.2,y=0; p=0.4,y=0} conf=0.3 acc=0   gap=0.3  权重 1/2
#                 箱1 {p=0.9,y=1; p=0.6,y=1} conf=0.75 acc=1 gap=0.25 权重 1/2
#                 ECE = 0.5*0.3 + 0.5*0.25 = 0.275
Y_HAND = [1, 0, 1, 0]
P_HAND = [0.9, 0.2, 0.6, 0.4]


def test_brier_hand_computed():
    assert brier_score(Y_HAND, P_HAND) == pytest.approx(0.0925, abs=1e-12)


def test_log_loss_hand_computed():
    expected = -(math.log(0.9) + math.log(0.8) + 2 * math.log(0.6)) / 4
    assert log_loss(Y_HAND, P_HAND) == pytest.approx(expected, abs=1e-12)


def test_ece_hand_computed():
    assert expected_calibration_error(Y_HAND, P_HAND, n_bins=2) == pytest.approx(
        0.275, abs=1e-12
    )


def test_bins_hand_computed():
    bins = calibration_bins(Y_HAND, P_HAND, n_bins=2)
    assert len(bins) == 2
    assert bins[0]["count"] == 2
    assert bins[0]["confidence"] == pytest.approx(0.3)
    assert bins[0]["accuracy"] == pytest.approx(0.0)
    assert bins[0]["gap"] == pytest.approx(0.3)
    assert bins[1]["confidence"] == pytest.approx(0.75)
    assert bins[1]["accuracy"] == pytest.approx(1.0)
    assert bins[1]["gap"] == pytest.approx(0.25)


# ------------------------------------------------------------ 加权复制等价
# 每条样本复制 k 次 == 该样本权重为 k,三个指标必须完全一致。
def test_weighted_replication_equivalence():
    p = [0.3, 0.7]
    y = [0, 1]
    w = [3, 2]
    p_rep = [0.3, 0.3, 0.3, 0.7, 0.7]
    y_rep = [0, 0, 0, 1, 1]

    assert brier_score(y, p, w) == pytest.approx(brier_score(y_rep, p_rep), abs=1e-12)
    assert log_loss(y, p, w) == pytest.approx(log_loss(y_rep, p_rep), abs=1e-12)
    for n_bins in (2, 5, 10):
        assert expected_calibration_error(y, p, w, n_bins=n_bins) == pytest.approx(
            expected_calibration_error(y_rep, p_rep, n_bins=n_bins), abs=1e-12
        )


def test_weighted_replication_equivalence_fractional():
    # 非整数权重:与按 10 倍放大后复制等价(0.5 -> 5 份, 1.5 -> 15 份)
    y = [1, 0]
    p = [0.8, 0.1]
    w = [0.5, 1.5]
    y_rep = [1] * 5 + [0] * 15
    p_rep = [0.8] * 5 + [0.1] * 15
    assert brier_score(y, p, w) == pytest.approx(brier_score(y_rep, p_rep), abs=1e-12)
    assert log_loss(y, p, w) == pytest.approx(log_loss(y_rep, p_rep), abs=1e-12)


def test_zero_weight_samples_are_ignored():
    y = [1, 0]
    p = [0.9, 0.9]  # 第二条权重为 0,不应影响结果
    w = [1.0, 0.0]
    assert brier_score(y, p, w) == pytest.approx(brier_score([1], [0.9]), abs=1e-12)
    assert log_loss(y, p, w) == pytest.approx(log_loss([1], [0.9]), abs=1e-12)


# ------------------------------------------------------------ 全同概率
def test_all_identical_probabilities():
    # 全部 p=0.5,y 正例率 0.6:所有样本落进同一箱
    y = [0, 1, 1, 0, 1]
    p = [0.5] * 5
    assert brier_score(y, p) == pytest.approx(0.25, abs=1e-12)
    assert log_loss(y, p) == pytest.approx(math.log(2), abs=1e-12)
    assert expected_calibration_error(y, p, n_bins=10) == pytest.approx(0.1, abs=1e-12)
    bins = calibration_bins(y, p, n_bins=10)
    nonempty = [b for b in bins if b["weight"] > 0]
    assert len(nonempty) == 1
    assert nonempty[0]["bin"] == 5  # 0.5 * 10 = 5,落在第 5 箱 [0.5, 0.6)


# ------------------------------------------------------------ 极端类别不均
def test_extreme_class_imbalance_all_positive():
    y = [1] * 1000
    p = [0.9] * 1000
    assert brier_score(y, p) == pytest.approx(0.01, abs=1e-12)
    assert log_loss(y, p) == pytest.approx(-math.log(0.9), abs=1e-12)
    assert expected_calibration_error(y, p, n_bins=10) == pytest.approx(0.1, abs=1e-12)


def test_extreme_class_imbalance_rare_positive():
    # 999 个负例 + 1 个正例,全部预测 0.001
    y = [0] * 999 + [1]
    p = [0.001] * 1000
    assert brier_score(y, p) == pytest.approx(
        (999 * 0.001**2 + (1 - 0.001) ** 2) / 1000, abs=1e-12
    )
    # 全部落在第 0 箱:acc = 0.001, conf = 0.001,ECE = 0
    assert expected_calibration_error(y, p, n_bins=10) == pytest.approx(0.0, abs=1e-12)


# ------------------------------------------------------------ 完美预测与端点
def test_perfect_prediction():
    y = [0, 1, 1, 0]
    p = [0.0, 1.0, 1.0, 0.0]
    assert brier_score(y, p) == pytest.approx(0.0, abs=1e-12)
    assert log_loss(y, p, endpoint_strategy="clip") == pytest.approx(0.0, abs=1e-9)
    assert expected_calibration_error(y, p, n_bins=10) == pytest.approx(0.0, abs=1e-12)


def test_endpoint_strategy_clip_vs_allow():
    # p=0 但 y=1:clip 给出有限损失,allow 给出 +inf
    assert log_loss([1], [0.0], endpoint_strategy="clip") == pytest.approx(
        -math.log(1e-15), rel=1e-9
    )
    assert math.isinf(log_loss([1], [0.0], endpoint_strategy="allow"))
    # p=0 且 y=0:两种策略都应为 0(0 * log 0 按 0 处理)
    assert log_loss([0], [0.0], endpoint_strategy="allow") == pytest.approx(0.0)


def test_bin_edge_endpoints():
    # p=0.0 进第一箱,p=1.0 进最后一箱(最后一箱含右端点)
    bins = calibration_bins([0, 1], [0.0, 1.0], n_bins=10)
    assert bins[0]["count"] == 1
    assert bins[9]["count"] == 1
    assert all(b["count"] == 0 for b in bins[1:9])


# ------------------------------------------------------------ 非法输入
@pytest.mark.parametrize("bad_p", [[-0.1, 0.5], [0.5, 1.2], [float("nan"), 0.5], [float("inf"), 0.5]])
def test_invalid_probabilities_rejected(bad_p):
    with pytest.raises(ValueError, match="y_prob"):
        brier_score([0, 1], bad_p)


def test_invalid_labels_rejected():
    with pytest.raises(ValueError, match="y_true"):
        brier_score([0, 2], [0.5, 0.5])


def test_length_mismatch_rejected():
    with pytest.raises(ValueError, match="长度不一致"):
        brier_score([0, 1], [0.5])


def test_empty_input_rejected():
    with pytest.raises(ValueError, match="不能为空"):
        brier_score([], [])


def test_negative_weight_rejected():
    with pytest.raises(ValueError, match="负权重"):
        brier_score([0, 1], [0.5, 0.5], [1.0, -0.5])


def test_zero_total_weight_rejected():
    with pytest.raises(ValueError, match="权重总和"):
        brier_score([0, 1], [0.5, 0.5], [0.0, 0.0])


def test_invalid_endpoint_strategy_rejected():
    with pytest.raises(ValueError, match="endpoint_strategy"):
        log_loss([0, 1], [0.5, 0.5], endpoint_strategy="bogus")


def test_invalid_n_bins_rejected():
    with pytest.raises(ValueError, match="n_bins"):
        calibration_bins([0, 1], [0.5, 0.5], n_bins=0)


# ------------------------------------------------------------ evaluate 汇总
def test_evaluate_returns_all_fields():
    r = evaluate(Y_HAND, P_HAND, n_bins=2)
    assert r["n_samples"] == 4
    assert r["total_weight"] == pytest.approx(4.0)
    assert r["brier_score"] == pytest.approx(0.0925)
    assert r["ece"] == pytest.approx(0.275)
    assert len(r["bins"]) == 2


def test_evaluate_inf_log_loss_serialized_as_string():
    r = evaluate([1], [0.0], endpoint_strategy="allow")
    assert r["log_loss"] == "inf"


# ------------------------------------------------------------ 合成数据冒烟
def test_synthetic_data_perfect_calibration_beats_miscalibrated():
    from calibration_eval.synthetic import make_synthetic, temperature_scale

    _, y, p_true = make_synthetic(n_samples=5000)
    ece_true = expected_calibration_error(y, p_true, n_bins=10)
    ece_miscal = expected_calibration_error(y, temperature_scale(p_true, 3.0), n_bins=10)
    assert ece_true < 0.02  # 完美校准参照应接近 0
    assert ece_miscal > ece_true + 0.05  # 温度缩放后明显失准
