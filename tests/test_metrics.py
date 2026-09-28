"""Metric kernel: hand-computed values and zero-denominator semantics."""
from __future__ import annotations

import pytest

from app.mining.rules import compute_metrics


def test_hand_computed_metrics_basic():
    # {beer} -> {diapers} on basic.json: counts ab=3, a=3, b=4, N=5
    m = compute_metrics(3, 3, 4, 5)
    assert m.support == pytest.approx(3 / 5)
    assert m.confidence == pytest.approx(1.0)
    assert m.lift == pytest.approx(1.25)
    assert m.leverage == pytest.approx(0.12)


def test_negative_leverage_for_mutually_exclusive_items():
    # exclusive.json: counts ab=0, a=2, b=2, N=4
    m = compute_metrics(0, 2, 2, 4)
    assert m.confidence == 0.0
    assert m.lift == 0.0
    assert m.leverage == pytest.approx(-0.25)


def test_zero_antecedent_count_makes_confidence_and_lift_undefined():
    # antecedent never occurs: confidence and lift undefined (None), not 0/NaN
    m = compute_metrics(0, 0, 3, 10)
    assert m.confidence is None
    assert m.lift is None
    assert m.support == 0.0
    assert m.leverage == pytest.approx(0.0 - 0.0 * 0.3)


def test_zero_consequent_count_makes_lift_undefined_only():
    m = compute_metrics(0, 4, 0, 10)
    assert m.confidence == 0.0
    assert m.lift is None
    assert m.leverage == pytest.approx(0.0)


def test_n_must_be_positive():
    with pytest.raises(ValueError):
        compute_metrics(0, 0, 0, 0)
