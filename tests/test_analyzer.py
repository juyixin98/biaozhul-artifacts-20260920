"""Tests for wrap/restart disambiguation, jumps, drift changes, few samples."""

from __future__ import annotations

from app.analyzer import analyze
from app.config import AnalyzerConfig
from tests.sim import make_observations

MOD = 10_000.0


def _types(result):
    return [e.type for e in result.events]


def test_clean_wraps_are_wrap_not_restart():
    obs = make_observations(n=80, modulus=6_000.0, seed=4)
    r = analyze(obs, device_id="d", modulus=6_000.0)
    assert "wrap" in _types(r)
    assert "restart" not in _types(r)
    assert "wrap_or_restart" not in _types(r)
    assert r.status == "ok"
    assert len(r.segments) == 1  # wraps unwrap into one segment


def test_restart_detected_when_counter_falls_far():
    obs = make_observations(n=60, modulus=100_000.0, restart_at=30, seed=4)
    r = analyze(obs, device_id="d", modulus=100_000.0)
    assert "restart" in _types(r)
    epochs = {s.epoch for s in r.segments}
    assert len(epochs) == 2
    # both halves share the same tick rate
    betas = [s.fit.beta_point for s in r.segments if s.fit.feasible]
    assert abs(betas[0] - betas[1]) / betas[0] < 1e-3


def test_unknown_modulus_backward_move_is_ambiguous():
    obs = make_observations(n=60, modulus=MOD, seed=4)
    r = analyze(obs, device_id="d", modulus=None)
    assert "wrap_or_restart" in _types(r)
    assert r.status == "uncertain"


def test_time_jump_event_with_indeterminate_attribution():
    obs = make_observations(
        n=60, modulus=100_000.0, jump_at=30, jump_s=3.0, seed=4
    )
    r = analyze(obs, device_id="d", modulus=100_000.0)
    jumps = [e for e in r.events if e.type == "time_jump"]
    assert len(jumps) == 1
    assert abs(jumps[0].detail["step_s"] - 3.0) < 0.05
    assert jumps[0].detail["attribution"] == "indeterminate"
    # slopes before and after agree
    slopes = [s.fit.beta_point for s in r.segments if s.fit.feasible]
    assert len(slopes) == 2
    assert abs(slopes[0] - slopes[1]) / slopes[0] < 1e-3


def test_drift_change_event_reports_two_rates():
    obs = make_observations(
        n=80, modulus=100_000.0, drift_kink=40, drift_ratio=1.5, seed=4
    )
    r = analyze(obs, device_id="d", modulus=100_000.0)
    dc = [e for e in r.events if e.type == "drift_change"]
    assert len(dc) == 1
    ratio = dc[0].detail["beta_ratio"]
    assert 1.4 < ratio < 1.6
    feas = [s for s in r.segments if s.fit.feasible]
    assert len(feas) == 2
    # continuous kink: vertical gap near zero
    assert abs(dc[0].detail["vertical_gap_at_break_s"]) < 0.1
    assert r.status == "ok"


def test_drift_change_is_not_confused_with_time_jump():
    obs = make_observations(
        n=80, modulus=100_000.0, drift_kink=40, drift_ratio=1.5, seed=4
    )
    r = analyze(obs, device_id="d", modulus=100_000.0)
    assert "time_jump" not in _types(r)


def test_few_samples_returns_insufficient_evidence():
    obs = make_observations(n=4, modulus=MOD, seed=4)
    r = analyze(obs, device_id="d", modulus=MOD)
    assert r.status == "insufficient_evidence"
    assert r.recommended_segment_id is None


def test_asymmetric_delay_does_not_bias_drift_rate():
    obs = make_observations(
        n=60,
        modulus=100_000.0,
        downlink_s=0.002,
        uplink_s=0.05,
        seed=4,
    )
    r = analyze(obs, device_id="d", modulus=100_000.0)
    assert r.status in {"ok", "uncertain"}
    seg = r.segments[r.recommended_segment_id]
    # true tick is 1 ms; the rate estimate must be accurate to 0.5 %
    assert abs(seg.fit.beta_point - 1e-3) / 1e-3 < 5e-3


def test_rtt_spikes_are_removed_before_fit():
    obs = make_observations(
        n=60, modulus=MOD, rtt_spikes=(12, 33, 47), seed=4
    )
    r = analyze(obs, device_id="d", modulus=MOD)
    assert set(r.n_rtt_dropped) == {12, 33, 47}
    seg = r.segments[r.recommended_segment_id]
    assert seg.fit.n_used == 57


def test_recv_before_send_rejected():
    import pytest

    from app.fitting import Observation

    obs = [Observation(1.0, 0.5, 0.0)]
    with pytest.raises(ValueError):
        analyze(obs, device_id="d", modulus=MOD)


def test_config_overrides_change_gate():
    obs = make_observations(n=12, modulus=100_000.0, seed=4)
    r = analyze(obs, device_id="d", modulus=100_000.0)
    # 12 samples fit but give a wide uncertainty interval -> uncertain
    assert r.status == "uncertain"
    permissive = AnalyzerConfig(
        offset_uncertainty_max_s=10.0,
        drift_uncertainty_max_rel=1.0,
        drift_uncertainty_max_abs=1.0,
    )
    r2 = analyze(
        obs, device_id="d", modulus=100_000.0, config=permissive
    )
    assert r2.status == "ok"
