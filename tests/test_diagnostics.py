"""Unit tests for overlap/weight/calibration diagnostic decisions."""

from __future__ import annotations

import numpy as np

from ipwate.config import DiagnosticsConfig
from ipwate.diagnostics import (
    aggregate_verdict,
    calibration_evidence,
    overlap_findings,
    run_diagnostics,
    summarize_weights,
)
from ipwate.statcontract import Verdict
from ipwate.weights import compute_weighted_estimation

_CFG = DiagnosticsConfig()


def _wr(a, y, p, **kw):
    from ipwate.config import ClippingConfig

    clip = ClippingConfig(enabled=kw.pop("clip", False))
    return compute_weighted_estimation(
        a, y, p,
        estimand=kw.get("estimand", "ate"),
        weight_type=kw.get("weight_type", "stabilized"),
        clipping=clip,
        positivity_eps=1e-6,
    )


def test_extreme_weights_trigger_reject_finding():
    # One untreated unit carries a near-zero denominator => huge weight.
    a = np.array([1] * 50 + [0] * 50, dtype=np.int8)
    rng = np.random.default_rng(0)
    y = rng.normal(size=100)
    p = np.full(100, 0.5)
    p[60] = 0.001  # untreated unit, 1-p ~ 0.999 -> weight ~1; treated p? set treated low
    # Force a huge treated weight instead: p tiny on a treated unit.
    p[0] = 0.002
    wr = _wr(a, y, p)
    ws = summarize_weights(a, wr)
    from ipwate.diagnostics import weight_findings

    findings = weight_findings(ws, _CFG)
    codes = {f.code for f in findings}
    assert "max_weight_reject" in codes or any(c.startswith("ess_reject") for c in codes)


def test_no_overlap_intervals_produce_reject():
    from ipwate.statcontract import OverlapSummary

    ov = OverlapSummary(
        pscore_min=0.01, pscore_max=0.99,
        treated_q05=0.8, treated_q95=0.95,
        untreated_q05=0.05, untreated_q95=0.2,
        treated_n=50, untreated_n=50, n_clipped=0, n_near_boundary=0,
    )
    findings = overlap_findings(ov, _CFG)
    assert any(f.code == "overlap_none" and f.status == "reject" for f in findings)


def test_verdict_priority_reject_over_warn():
    from ipwate.statcontract import DiagnosticFinding

    findings = [
        DiagnosticFinding("a", "warn", "warn", "m"),
        DiagnosticFinding("b", "reject", "reject", "m"),
        DiagnosticFinding("c", "info", "accept", "m"),
    ]
    assert aggregate_verdict(findings) is Verdict.REJECT


def test_calibration_inconclusive_on_small_sample():
    a = np.array([0, 1] * 30, dtype=np.int8)
    p = np.linspace(0.1, 0.9, 60)
    evidence, findings = calibration_evidence(a, p, _CFG)
    assert evidence["assessed"] is False
    assert findings[0].status == "inconclusive"


def test_well_specified_scores_pass_calibration_on_large_sample():
    rng = np.random.default_rng(7)
    p = np.clip(rng.beta(5, 5, size=3000), 0.02, 0.98)
    a = (rng.uniform(size=3000) < p).astype(np.int8)
    evidence, findings = calibration_evidence(a, p, _CFG)
    assert evidence["assessed"] is True
    assert not any(f.severity == "reject" for f in findings)
    assert 0.8 < evidence["calibration_slope"] < 1.2


def test_run_diagnostics_attaches_all_sections_and_a_verdict():
    rng = np.random.default_rng(3)
    n = 400
    a = (rng.uniform(size=n) < 0.5).astype(np.int8)
    p = np.clip(0.5 + 0.2 * rng.normal(size=n), 0.05, 0.95)
    y = rng.normal(size=n)
    wr = _wr(a, y, p)
    verdict, ws, ov, cal, findings = run_diagnostics(a, wr, _CFG)
    assert verdict in tuple(Verdict)
    assert ws.overall_ess_fraction > 0
    assert ov.treated_n + ov.untreated_n == n
    assert isinstance(findings, list)
