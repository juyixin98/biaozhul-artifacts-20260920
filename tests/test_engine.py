"""Engine tests: path selection and full cache binding semantics."""

from __future__ import annotations

import numpy as np

from toeplitz_fft.config import Settings
from toeplitz_fft.engine import Engine
from toeplitz_fft.fixtures import (
    fixture_to_payload,
    make_asymmetric_real,
    make_tiny_case,
)
from toeplitz_fft.inputs import parse_problem
from toeplitz_fft.kernels import coefficient_fingerprint, padded_embedding_size


def _engine() -> Engine:
    return Engine(Settings(
        max_n=65_536, max_batch=256, cache_size=4, rtol_bits=64.0,
        tiny_n_explicit=2, oracle_mpmath_prec=60, dense_oracle_max_n=128,
        log_level="WARNING"))


def test_tiny_problem_uses_explainable_path() -> None:
    engine = _engine()
    problem = parse_problem(fixture_to_payload(make_tiny_case(1)))
    result = engine.run(problem)
    assert result.plan.path == "tiny_explicit"
    np.testing.assert_allclose(result.output, [[7.0], [-14.0]])


def test_repeated_call_is_cache_hit() -> None:
    engine = _engine()
    problem = parse_problem(fixture_to_payload(make_asymmetric_real(13)))
    first = engine.run(problem)
    second = engine.run(problem)
    assert first.cache_hit is False
    assert second.cache_hit is True
    assert engine.cache_info()["hits"] == 1
    np.testing.assert_array_equal(first.output, second.output)


def test_different_coefficients_same_shape_do_not_share_plan() -> None:
    engine = _engine()
    fx_a = make_asymmetric_real(13, seed=1)
    fx_b = make_asymmetric_real(13, seed=2)
    r_a = engine.run(parse_problem(fixture_to_payload(fx_a)))
    r_b = engine.run(parse_problem(fixture_to_payload(fx_b)))
    assert r_a.cache_hit is False
    assert r_b.cache_hit is False
    assert r_a.plan.coefficient_digest != r_b.plan.coefficient_digest


def test_cache_key_includes_full_size() -> None:
    engine = _engine()
    fx13 = make_asymmetric_real(13, seed=5)
    fx17 = make_asymmetric_real(17, seed=5)
    r1 = engine.run(parse_problem(fixture_to_payload(fx13)))
    r2 = engine.run(parse_problem(fixture_to_payload(fx17)))
    assert r1.plan.m == padded_embedding_size(13)
    assert r2.plan.m == padded_embedding_size(17)
    assert r2.cache_hit is False


def test_cache_eviction_respects_capacity() -> None:
    engine = _engine()
    results = []
    for seed in range(6):  # capacity is 4
        fx = make_asymmetric_real(13, seed=100 + seed)
        results.append(engine.run(parse_problem(fixture_to_payload(fx))))
    assert engine.cache_info()["entries"] == 4
    # Re-running the most recent entry still hits.
    fx = make_asymmetric_real(13, seed=105)
    assert engine.run(parse_problem(fixture_to_payload(fx))).cache_hit is True


def test_kernel_digest_is_stable_across_engines() -> None:
    assert _engine().kernel_summary() == _engine().kernel_summary()


def test_force_embedding_path_on_tiny_problem() -> None:
    engine = _engine()
    problem = parse_problem(
        fixture_to_payload(make_tiny_case(1), kernel="embedding_fft"))
    result = engine.run(problem)
    assert result.plan.path == "embedding_fft"
    np.testing.assert_allclose(result.output, [[7.0], [-14.0]], atol=1e-12)


def test_fingerprint_changes_with_embedding_size() -> None:
    rng = np.random.default_rng(0)
    c = rng.standard_normal(5)
    r = c.copy()
    d1 = coefficient_fingerprint(c, r, 9)
    d2 = coefficient_fingerprint(c, r, 16)
    assert d1 != d2
    # Content change changes the digest too.
    c2 = c.copy()
    c2[-1] += 1.0
    assert coefficient_fingerprint(c2, r, 9) != d1
