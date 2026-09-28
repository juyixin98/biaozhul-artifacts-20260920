"""Long-time integration: segmentation, composition consistency, restarts."""

from __future__ import annotations

import numpy as np

from krylov_expv.config import ExpvConfig
from krylov_expv.core.integrator import expv

from .conftest import load_fixture
from .reference import reference_expv, relative_error


def test_long_time_requires_and_uses_multiple_segments():
    payload, A, v = load_fixture("advection60_long")
    t = payload["t"]
    result = expv(A, t, v, tol=payload["tol"])
    ref = reference_expv(A, t, v)

    assert result.converged
    assert relative_error(result.w, ref) < 1e-7
    # t*||A|| ~ 120 cannot be one m_max=30 Krylov step: segmentation happened
    assert result.evidence.num_steps > 1
    # the segment trace is a complete partition of [0, t]
    assert abs(sum(s.tau for s in result.evidence.steps) - t) < 1e-9
    for s1, s2 in zip(result.evidence.steps, result.evidence.steps[1:]):
        assert np.isclose(s1.t_before + s1.tau, s2.t_before)


def test_segmented_composition_matches_single_call():
    # exp((t1+t2) A) v == exp(t2 A) (exp(t1 A) v), computed as two restarts
    payload, A, v = load_fixture("advection60_long")
    t1, t2 = 50.0, 70.0
    cfg = ExpvConfig(tol=payload["tol"])

    first = expv(A, t1, v, config=cfg)
    second = expv(A, t2, first.w, config=cfg)
    direct = expv(A, t1 + t2, v, config=cfg)

    assert first.converged and second.converged and direct.converged
    rel = np.linalg.norm(second.w - direct.w) / np.linalg.norm(direct.w)
    assert rel < 1e-6


def test_error_budget_is_respected_and_segments_accumulate():
    # the accumulated estimate is the sum of per-segment estimates and stays
    # within the tolerance budget; longer horizons use more segments
    payload, A, v = load_fixture("advection60_long")
    short = expv(A, 10.0, v, tol=payload["tol"])
    long = expv(A, payload["t"], v, tol=payload["tol"])

    assert long.evidence.num_steps > short.evidence.num_steps
    for result in (short, long):
        per_step_sum = sum(s.error_estimate for s in result.evidence.steps)
        assert result.evidence.total_error_estimate == per_step_sum
        budget = payload["tol"] * max(float(np.linalg.norm(result.w)), 1e-300)
        assert result.evidence.total_error_estimate <= budget
