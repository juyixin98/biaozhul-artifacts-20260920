"""Tests for post-quantization stability and limit-cycle analysis."""

import numpy as np
import pytest

from fixed_iir.biquad import FilterConfig, SOS
from fixed_iir.design import butter_lowpass
from fixed_iir.qformat import QFormat
from fixed_iir.stability import (
    assess_stability,
    pole_radius_report,
    simulate_zero_input,
)


def test_jury_and_radii_stable_filter():
    sos = butter_lowpass(4, 0.2)
    rep = pole_radius_report(sos)
    assert rep.unstable_sections == []
    for s in rep.sections:
        assert s.jury_stable
        assert all(r < 1.0 for r in s.radii)
        jv = s.jury_values
        assert jv["abs_a2"] < 1.0
        assert jv["one_plus_a1_plus_a2"] > 0
        assert jv["one_minus_a1_plus_a2"] > 0


def test_explicitly_unstable_float_filter_flagged():
    # pole at z = 1.5
    sos = SOS(np.array([[1.0, 0.0, 0.0, 1.0, -1.5, 0.0]]))
    rep = pole_radius_report(sos)
    assert rep.unstable_sections == [0]
    assert rep.max_radius == pytest.approx(1.5)


def test_near_unit_circle_section_is_warning():
    # poles at r = sqrt(0.98) ~= 0.9899: stable but close to the edge
    sos = SOS(np.array([[0.01, 0.0, 0.0, 1.0, -0.5, 0.98]]))
    rep = assess_stability(sos)
    assert rep.verdict in ("warning",)
    assert any("unit circle" in w for w in rep.warnings)


# ---------------------------------------------------------------------------
# Counterexample: float-stable, strictly divergent after floor quantization
# ---------------------------------------------------------------------------

FLOOR_CE = SOS(np.array([[0.25, 0.0, -0.25, 1.0, -1.9, 0.91]]))
FLOOR_CFG = FilterConfig(
    q_sig=QFormat(1, 5),
    q_coef=QFormat(2, 2),
    guard_bits=1,
    rounding="floor",
)


def test_floor_counterexample_float_is_stable():
    rep = pole_radius_report(FLOOR_CE)
    assert rep.sections[0].jury_stable
    assert rep.max_radius == pytest.approx(np.sqrt(0.91), abs=1e-9)


def test_floor_counterexample_quantized_is_unstable():
    rep = pole_radius_report(FLOOR_CE.quantize(FLOOR_CFG.q_coef, "floor"))
    assert not rep.sections[0].jury_stable
    radii = sorted(rep.sections[0].radii)
    assert max(radii) == pytest.approx(1.5, abs=1e-12)  # poles 0.5 and 1.5


def test_floor_counterexample_assessment_unsafe():
    rep = assess_stability(FLOOR_CE, FLOOR_CFG)
    assert rep.verdict == "unsafe"
    assert rep.poles_quantized.unstable_sections == [0]
    assert any("outside unit circle" in w for w in rep.warnings)


# ---------------------------------------------------------------------------
# Counterexample: wrap overflow -> large zero-input limit cycle
# ---------------------------------------------------------------------------

WRAP_CE = SOS(np.array([[0.1, 0.0, 0.0, 1.0, -1.8, 0.95]]))
WRAP_CFG = FilterConfig(
    q_sig=QFormat(1, 15),
    q_coef=QFormat(2, 14),
    guard_bits=0,
    rounding="convergent",
    overflow="wrap",
)
SAT_CFG = FilterConfig(
    q_sig=QFormat(1, 15),
    q_coef=QFormat(2, 14),
    guard_bits=0,
    rounding="convergent",
    overflow="saturate",
)


def test_wrap_counterexample_linear_coefficients_stable():
    rep = pole_radius_report(WRAP_CE.quantize(WRAP_CFG.q_coef, "convergent"))
    assert rep.unstable_sections == []
    assert rep.max_radius < 1.0


def test_wrap_produces_large_overflow_cycle_saturate_does_not():
    cyc_wrap = simulate_zero_input(WRAP_CE, WRAP_CFG)
    cyc_sat = simulate_zero_input(WRAP_CE, SAT_CFG)
    w = cyc_wrap[0]
    s = cyc_sat[0]
    assert w.found and w.kind == "overflow"
    assert w.amplitude > 0.25  # large-scale spurious oscillation
    assert w.period >= 2
    # Saturation suppresses the same large cycle (any residual cycle is the
    # much smaller granular kind).
    assert (not s.found) or s.amplitude < 0.05


def test_guard_bits_remove_wrap_overflow_cycle():
    # With 2 headroom bits, 1x full-scale excitation never overflows even
    # under wrap arithmetic (verified empirically: 0 overflow events).
    cfg = FilterConfig(
        q_sig=QFormat(1, 15), q_coef=QFormat(2, 14), guard_bits=2,
        rounding="convergent", overflow="wrap",
    )
    cyc = simulate_zero_input(WRAP_CE, cfg, multiples=(1,))
    assert (not cyc[0].found) or cyc[0].kind != "overflow"


# ---------------------------------------------------------------------------
# Benign reference design under a sane word length
# ---------------------------------------------------------------------------


def test_well_scaled_butterworth_is_ok():
    sos = butter_lowpass(4, 0.2)
    cfg = FilterConfig(q_sig=QFormat(1, 15), q_coef=QFormat(2, 14))
    rep = assess_stability(sos, cfg)
    assert rep.verdict in ("ok", "warning")  # granular cycles may warn
    assert rep.poles_quantized.unstable_sections == []
