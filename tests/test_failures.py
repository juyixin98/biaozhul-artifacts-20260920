"""Failure classes: non-convergence and budget rejection are explicit."""

from __future__ import annotations

import numpy as np
import pytest

from krylov_expv.config import ExpvConfig
from krylov_expv.core.integrator import expv
from krylov_expv.errors import MemoryBudgetExceeded

from .conftest import load_fixture


def test_max_steps_exceeded_is_reported_not_hidden():
    payload, A, v = load_fixture("advection60_long")
    cfg = ExpvConfig(tol=payload["tol"], max_steps=2, m_max=8)
    result = expv(A, payload["t"], v, config=cfg)

    assert not result.converged
    assert result.evidence.termination == "failed"
    assert result.evidence.failure_category == "max_steps_exceeded"
    assert "tolerance" in result.evidence.failure_message
    # partial progress is still returned and accounted for
    assert result.evidence.num_steps == 2
    covered = sum(s.tau for s in result.evidence.steps)
    assert 0.0 < covered < payload["t"]


def test_memory_budget_rejection_happens_before_any_work():
    payload, A, v = load_fixture("advection60_long")
    cfg = ExpvConfig(memory_budget_bytes=64)  # absurdly small
    with pytest.raises(MemoryBudgetExceeded) as excinfo:
        expv(A, payload["t"], v, config=cfg)
    assert excinfo.value.category == "memory_budget_exceeded"
    assert "bytes" in excinfo.value.message


def test_basis_memory_accounting_includes_basis_vectors():
    cfg = ExpvConfig(m_max=30)
    n = 100
    expected = (30 + 1) * n * 8 + (30 + 1) * 30 * 8 + 4 * n * 8
    assert cfg.basis_memory_bytes(n) == expected


def test_step_size_underflow_category_exists():
    # force the halving cap to zero: any rejected first step must underflow
    payload, A, v = load_fixture("nonnormal8")
    cfg = ExpvConfig(tol=1e-14, m_max=2, max_halvings=0, max_steps=10)
    result = expv(A, payload["t"], v, config=cfg)
    assert not result.converged
    assert result.evidence.failure_category == "step_size_underflow"
