"""SOC clamping at configured bounds."""
import pytest

from app.engine import F_SOC_CLAMPED_HIGH, F_SOC_CLAMPED_LOW, Sample, estimate


def test_clamp_low(params):
    samples = [Sample(float(t), 50.0, 3.0, 25.0) for t in range(0, 3601)]
    res = estimate(samples, params, initial_soc=0.05)
    assert res["summary"]["soc"] == params.soc_min == 0.0
    assert F_SOC_CLAMPED_LOW in res["trace"][-1]["flags"]
    assert any(e["type"] == "soc_clamp" for e in res["events"])


def test_clamp_high(params):
    samples = [Sample(float(t), -50.0, 4.2, 25.0) for t in range(0, 3601)]
    res = estimate(samples, params, initial_soc=0.95)
    assert res["summary"]["soc"] == params.soc_max == 1.0
    assert F_SOC_CLAMPED_HIGH in res["trace"][-1]["flags"]


def test_never_exceeds_bounds(params):
    samples = [Sample(float(t), 100.0, 3.0, 25.0) for t in range(0, 7201)]
    res = estimate(samples, params, initial_soc=0.5)
    assert all(0.0 <= row["soc"] <= 1.0 for row in res["trace"])
