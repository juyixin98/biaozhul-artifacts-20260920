"""Cache tests: the key binds full sizes and kernel content digests."""

import numpy as np

from toeplitz_fft import PlanCache, ToeplitzConfig, matvec
from toeplitz_fft.cache import kernel_digest


def _case(seed=41, m=6, n=4):
    g = np.random.default_rng(seed)
    c = g.standard_normal(m)
    r = g.standard_normal(n)
    r[0] = c[0]
    return c, r, g.standard_normal(n)


def test_repeated_call_hits_cache_and_returns_identical_result():
    cache = PlanCache(maxsize=8)
    c, r, x = _case()
    y1, meta1 = matvec(c, r, x, cache=cache)
    y2, meta2 = matvec(c, r, x, cache=cache)
    assert meta1["cache_hit"] is False
    assert meta2["cache_hit"] is True
    assert meta1["kernel_digest"] == meta2["kernel_digest"]
    np.testing.assert_array_equal(y1, y2)
    assert cache.stats()["hits"] == 1
    assert cache.stats()["misses"] == 1


def test_changed_kernel_content_misses_cache():
    cache = PlanCache(maxsize=8)
    c, r, x = _case()
    matvec(c, r, x, cache=cache)
    r2 = r.copy()
    r2[1] += 1.0  # same sizes, different content -> different digest
    _, meta = matvec(c, r2, x, cache=cache)
    assert meta["cache_hit"] is False
    assert cache.stats()["misses"] == 2


def test_same_content_different_sizes_misses_cache():
    cache = PlanCache(maxsize=8)
    c, r, x = _case()
    matvec(c, r, x, cache=cache)
    c2 = np.concatenate([c, [0.5]])  # m grows by one
    _, meta = matvec(c2, r, x, cache=cache)
    assert meta["cache_hit"] is False


def test_mode_and_dtype_are_part_of_the_key():
    cache = PlanCache(maxsize=8)
    c, r, x = _case()
    matvec(c, r, x, mode="real", cache=cache)
    _, meta = matvec(c, r, x, mode="complex", cache=cache)
    assert meta["cache_hit"] is False  # same bytes, different mode -> miss


def test_digest_differs_when_any_bound_field_changes():
    c, r, _ = _case()
    base = kernel_digest(c, r, mode="real", L=9, real_dtype="float64",
                         complex_dtype="complex128")
    assert base != kernel_digest(c, r, mode="complex", L=9,
                                 real_dtype="float64", complex_dtype="complex128")
    assert base != kernel_digest(c, r, mode="real", L=16,
                                 real_dtype="float64", complex_dtype="complex128")
    assert base != kernel_digest(c, r, mode="real", L=9,
                                 real_dtype="float32", complex_dtype="complex64")


def test_lru_eviction_bounds_size():
    cache = PlanCache(maxsize=2)
    g = np.random.default_rng(7)
    for i in range(4):
        c = g.standard_normal(3)
        r = g.standard_normal(3)
        r[0] = c[0]
        matvec(c, r, g.standard_normal(3), cache=cache)
    assert cache.stats()["size"] == 2
