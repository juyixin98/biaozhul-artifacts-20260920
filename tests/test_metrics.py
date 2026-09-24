"""精确率/召回率指标的单元测试（含 undecidable 的拒判语义）。"""

import pytest

from groundseg.metrics import evaluate
from groundseg.segment import GROUND, NON_GROUND, UNDECIDED


def test_perfect_classification():
    pred = [GROUND, GROUND, NON_GROUND, NON_GROUND]
    truth = [1, 1, 0, 0]
    m = evaluate(pred, truth)["ground"]
    assert m["precision"] == 1.0
    assert m["recall"] == 1.0
    assert m["undecided_rate"] == 0.0
    assert m["confusion"] == {"tp": 2, "fp": 0, "fn": 0, "tn": 2,
                              "undecided": 0, "total": 4}


def test_false_positive_and_false_negative():
    pred = [GROUND, NON_GROUND, GROUND, NON_GROUND]
    truth = [1, 1, 0, 0]
    # 点1 是地面却判非地面(FN)，点3 非地面却判地面(FP)
    m = evaluate(pred, truth)["ground"]
    assert m["tp"] == 1
    assert m["fp"] == 1
    assert m["fn"] == 1
    assert m["tn"] == 1
    assert m["precision"] == 0.5
    assert m["recall"] == 0.5


def test_undecided_counts_as_miss_in_recall_but_not_false_positive():
    pred = [GROUND, UNDECIDED, UNDECIDED, NON_GROUND]
    truth = [1, 1, 0, 0]
    m = evaluate(pred, truth)["ground"]
    # 点2 地面被拒判 -> recall 里算 FN；点3 非地面被拒判 -> 不算 FP
    assert m["tp"] == 1
    assert m["fp"] == 0
    assert m["fn"] == 1
    assert m["recall"] == 0.5
    assert m["precision"] == 1.0
    assert m["undecided_rate"] == 0.5
    # decided_recall：在做出判定的地面点（只有点1）上，判对率 1.0
    assert m["decided_recall"] == 1.0


def test_decided_recall_distinguishes_wrong_from_abstain():
    pred = [GROUND, NON_GROUND, UNDECIDED]
    truth = [1, 1, 1]
    m = evaluate(pred, truth)["ground"]
    # 三个地面点：一个判对，一个判错，一个拒判
    assert m["recall"] == pytest.approx(1 / 3)
    assert m["decided_recall"] == 0.5  # 判错不享受拒判保护


def test_non_ground_metrics_are_symmetric():
    pred = [GROUND, NON_GROUND, NON_GROUND]
    truth = [0, 0, 1]
    m = evaluate(pred, truth)["non_ground"]
    assert m["tp"] == 1      # 点2 非地面判非地面
    assert m["fp"] == 1      # 点1 真值地面判非地面
    assert m["fn"] == 1      # 点3 真值非地面判地面


def test_length_mismatch_raises():
    with pytest.raises(ValueError):
        evaluate([GROUND, NON_GROUND], [1, 0, 1])


def test_accepts_segment_result_like():
    class R:
        labels = [GROUND, NON_GROUND]
    m = evaluate(R(), [1, 0])["ground"]
    assert m["precision"] == 1.0
    assert m["recall"] == 1.0


def test_boolean_and_int_truth_forms():
    m1 = evaluate([GROUND, NON_GROUND], [True, False])["ground"]
    m2 = evaluate([GROUND, NON_GROUND], [1, 0])["ground"]
    assert m1["confusion"] == m2["confusion"]
