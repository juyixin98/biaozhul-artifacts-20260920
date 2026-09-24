"""Charge/discharge switching: sign convention and coulombic efficiency."""
import pytest

from app.engine import Sample, estimate

Q_AH = 50.0
ETA_CHG = 0.995


def _run(currents, params, initial_soc=0.5):
    """currents: list of (duration_s, current_a) segments, 1 s sampling."""
    samples = []
    t = 0
    for duration, cur in currents:
        for k in range(duration):
            samples.append(Sample(float(t + k), cur, 3.8, 25.0))
        t += duration
    return estimate(samples, params, initial_soc=initial_soc)


def test_discharge_decreases_soc(params):
    res = _run([(600, 10.0)], params)
    # 599 integrated seconds (first sample has dt=0)
    expected = 0.5 - 10.0 * 599 / (3600.0 * Q_AH)
    assert res["summary"]["soc"] == pytest.approx(expected, abs=1e-12)
    assert res["summary"]["soc"] < 0.5


def test_charge_increases_soc_with_efficiency(params):
    res = _run([(600, -10.0)], params)
    expected = 0.5 + 10.0 * 599 * ETA_CHG / (3600.0 * Q_AH)
    assert res["summary"]["soc"] == pytest.approx(expected, abs=1e-12)
    assert res["summary"]["soc"] > 0.5


def test_switching_is_not_symmetric_due_to_efficiency(params):
    """Equal Ah out then in must NOT return to the start: charge eta < 1."""
    res = _run([(600, 10.0), (600, -10.0)], params)
    discharged = 10.0 * 599 / (3600.0 * Q_AH)
    charged = 10.0 * 600 * ETA_CHG / (3600.0 * Q_AH)
    expected = 0.5 - discharged + charged
    assert res["summary"]["soc"] == pytest.approx(expected, abs=1e-12)
    assert res["summary"]["soc"] < 0.5  # net loss because eta_charge < 1


def test_direction_signs(params):
    down = _run([(300, 5.0)], params)["summary"]["soc"]
    up = _run([(300, -5.0)], params)["summary"]["soc"]
    assert down < 0.5 < up
