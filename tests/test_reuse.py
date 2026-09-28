"""Symbolic-structure reuse rules and pattern-change invalidation."""
from __future__ import annotations

import numpy as np

from app.numerical_input.fixtures import banded, pattern_swap_pair


def test_same_pattern_new_values_reuses_symbolic(engine):
    fx = banded(20)
    r1 = engine.solve(fx.n, fx.rows, fx.cols, fx.vals, fx.rhs,
                      ordering="minimum_degree")
    assert r1.report.pattern_reused is False
    # perturb numerical values while keeping coordinates identical
    rng = np.random.default_rng(3)
    new_vals = fx.vals.copy()
    diag_mask = fx.rows == fx.cols
    new_vals[diag_mask] += rng.uniform(1.0, 2.0, size=diag_mask.sum())
    r2 = engine.solve(fx.n, fx.rows, fx.cols, new_vals, fx.rhs,
                      ordering="minimum_degree")
    assert r2.report.pattern_reused is True
    assert r2.report.pattern_fingerprint == r1.report.pattern_fingerprint


def test_pattern_change_forces_new_symbolic(engine):
    a, b = pattern_swap_pair(12)
    ra = engine.solve(a.n, a.rows, a.cols, a.vals, a.rhs,
                      ordering="minimum_degree")
    rb = engine.solve(b.n, b.rows, b.cols, b.vals, b.rhs,
                      ordering="minimum_degree")
    assert ra.report.pattern_reused is False
    assert rb.report.pattern_reused is False
    assert ra.report.pattern_fingerprint != rb.report.pattern_fingerprint


def test_reuse_only_within_same_ordering(engine):
    fx = banded(16)
    engine.solve(fx.n, fx.rows, fx.cols, fx.vals, fx.rhs,
                 ordering="natural")
    r_md = engine.solve(fx.n, fx.rows, fx.cols, fx.vals, fx.rhs,
                        ordering="minimum_degree")
    assert r_md.report.pattern_reused is False
    r_nat = engine.solve(fx.n, fx.rows, fx.cols, fx.vals, fx.rhs,
                         ordering="natural")
    assert r_nat.report.pattern_reused is True


def test_different_dimension_not_reused(engine):
    f1 = banded(8)
    f2 = banded(10)
    engine.solve(f1.n, f1.rows, f1.cols, f1.vals, f1.rhs)
    r2 = engine.solve(f2.n, f2.rows, f2.cols, f2.vals, f2.rhs)
    assert r2.report.pattern_reused is False
