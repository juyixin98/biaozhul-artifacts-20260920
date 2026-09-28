"""Tests for symbolic-structure reuse: only exact pattern match is allowed."""
from __future__ import annotations

import numpy as np
import pytest

from config.settings import FactorizationConfig
from sparse_cholesky.core import (
    FactorizationEngine,
    SymbolicCache,
    pattern_fingerprint,
)
from sparse_cholesky.input.errors import SymbolicStructureMismatchError


def test_cache_misses_then_hits_on_identical_pattern(pattern_pair):
    matrix_a, _ = pattern_pair
    engine = FactorizationEngine(FactorizationConfig())
    first, _ = engine.factor(matrix_a)
    assert first.cache_hit is False
    second, _ = engine.factor(matrix_a)
    assert second.cache_hit is True


def test_different_values_same_pattern_reuse_structure(pattern_pair):
    matrix_a, _ = pattern_pair
    # Scale all values: same support, different numerics -> reuse is valid.
    scaled = matrix_a.csc * 3.0
    from sparse_cholesky.input.matrix import SparseMatrix

    scaled_matrix = SparseMatrix(
        n=matrix_a.n,
        csc=scaled.tocsc(),
        lower_csr=scaled.tocsr(),
        nnz_input=matrix_a.nnz_input,
    )
    assert pattern_fingerprint(scaled_matrix.csc) == pattern_fingerprint(
        matrix_a.csc
    )
    engine = FactorizationEngine(FactorizationConfig())
    engine.factor(matrix_a)
    result, _ = engine.factor(scaled_matrix)
    assert result.cache_hit is True


def test_pattern_change_is_not_reused(pattern_pair):
    matrix_a, matrix_b = pattern_pair
    assert pattern_fingerprint(matrix_a.csc) != pattern_fingerprint(matrix_b.csc)
    engine = FactorizationEngine(FactorizationConfig())
    engine.factor(matrix_a)
    result_b, _ = engine.factor(matrix_b)
    # Different pattern must be computed afresh, never silently reused.
    assert result_b.cache_hit is False


def test_forced_handle_on_changed_pattern_raises(pattern_pair):
    matrix_a, matrix_b = pattern_pair
    engine = FactorizationEngine(FactorizationConfig())
    handle = engine.symbolic_for(matrix_a)
    with pytest.raises(SymbolicStructureMismatchError) as exc:
        engine.factor(matrix_b, symbolic_handle=handle)
    assert exc.value.error_type == "symbolic_structure_mismatch_error"


def test_forced_handle_on_matching_pattern_succeeds(pattern_pair):
    matrix_a, _ = pattern_pair
    engine = FactorizationEngine(FactorizationConfig())
    handle = engine.symbolic_for(matrix_a)
    result, _ = engine.factor(matrix_a, symbolic_handle=handle)
    assert result.cache_hit is True


def test_cache_is_independent_per_ordering(pattern_pair):
    matrix_a, _ = pattern_pair
    engine = FactorizationEngine(FactorizationConfig())
    nat, _ = engine.factor(matrix_a, ordering="natural")
    rcm, _ = engine.factor(matrix_a, ordering="rcm")
    assert nat.cache_hit is False
    assert rcm.cache_hit is False  # different ordering key
    nat2, _ = engine.factor(matrix_a, ordering="natural")
    assert nat2.cache_hit is True


def test_cache_disabled_via_config(pattern_pair):
    matrix_a, _ = pattern_pair
    cfg = FactorizationConfig(symbolic_cache_enabled=False)
    engine = FactorizationEngine(cfg)
    engine.factor(matrix_a)
    result, _ = engine.factor(matrix_a)
    assert result.cache_hit is False


def test_cache_hit_miss_counts(pattern_pair):
    matrix_a, _ = pattern_pair
    cache = SymbolicCache()
    engine = FactorizationEngine(FactorizationConfig(), cache=cache)
    engine.factor(matrix_a)
    engine.factor(matrix_a)
    assert cache.hits == 1
    assert cache.misses == 1
