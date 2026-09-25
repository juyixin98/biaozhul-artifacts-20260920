"""Tests for fixed-point Q-format primitives."""

import numpy as np
import pytest

from fixed_iir.qformat import (
    Q15,
    Q31,
    QFormat,
    add_q,
    mul_q,
    saturate,
    shift_right_rounded,
    to_fixed,
    to_float,
    wrap_int,
    wrapped,
)


def test_qformat_ranges():
    assert Q15.qmax == 32767
    assert Q15.qmin == -32768
    assert Q15.scale == 32768.0
    assert str(Q15) == "Q0.15"
    assert str(QFormat(2, 14)) == "Q1.14"
    assert Q31.qmax == 2147483647
    assert Q31.qmin == -2147483648


def test_qformat_validation():
    with pytest.raises(ValueError):
        QFormat(0, 16)
    with pytest.raises(ValueError):
        QFormat(1, -1)
    with pytest.raises(ValueError):
        QFormat(2, 31)  # 33 bits


def test_to_fixed_exact_values():
    r = to_fixed([0.5, -0.5, 0.0, 0.25], Q15)
    assert list(r.fixed) == [16384, -16384, 0, 8192]
    assert r.saturated_count == 0
    assert r.max_abs_err == 0.0
    # -1.0 is representable, +1.0 is not (saturates to +1-2^-15)
    r2 = to_fixed([-1.0, 1.0], Q15)
    assert list(r2.fixed) == [-32768, 32767]
    assert r2.saturated_count == 1


def test_to_fixed_rounding_modes():
    q = QFormat(3, 1)  # range [-4, 3.5], values step 0.5
    # 0.25 -> 0.5 LSB: tie between 0.0 and 0.5
    assert to_fixed(0.25, q, "convergent").fixed.item() == 0   # -> 0.0 (even)
    assert to_fixed(0.75, q, "convergent").fixed.item() == 2   # 1.5 LSB -> 1.0 (even)
    assert to_fixed(0.25, q, "half_up").fixed.item() == 1       # -> 0.5
    assert to_fixed(-0.25, q, "half_up").fixed.item() == -1     # -> -0.5
    assert to_fixed(1.2, q, "truncate").fixed.item() == 2       # -> 1.0
    assert to_fixed(-1.2, q, "truncate").fixed.item() == -2     # toward zero
    with pytest.raises(ValueError):
        to_fixed(1.0, Q15, "bogus")


def test_round_trip_quantization_error():
    x = np.linspace(-0.9, 0.9, 1001)
    r = to_fixed(x, Q15)
    assert r.max_abs_err <= 0.5 / 32768.0
    np.testing.assert_allclose(to_float(r.fixed, Q15), r.values)


def test_saturate():
    out, n = saturate(np.array([40000, -40000, 100, -32768], dtype=np.int64), Q15)
    assert list(out) == [32767, -32768, 100, -32768]
    assert n == 2


def test_mul_q_basic():
    a = to_fixed([0.5, -0.5, 0.5], Q15).fixed
    b = to_fixed([0.5, 0.5, -0.5], Q15).fixed
    m = mul_q(a, b, Q15, "convergent")
    np.testing.assert_array_equal(m, [8192, -8192, -8192])
    np.testing.assert_allclose(to_float(m, Q15), [0.25, -0.25, -0.25], atol=1e-9)


def test_mul_q_convergent_tie():
    # 16384 * 3 = 49152 = 1.5 units after >>15; half-to-even -> 2
    a = np.array([16384], dtype=np.int64)
    b = np.array([3], dtype=np.int64)
    assert mul_q(a, b, Q15, "convergent").item() == 2
    assert mul_q(a, b, Q15, "half_up").item() == 2
    # 16384 * 1 = 16384 = 0.5 units; half-to-even -> 0, half-up -> 1
    assert mul_q(a, np.array([1], dtype=np.int64), Q15, "convergent").item() == 0
    assert mul_q(a, np.array([1], dtype=np.int64), Q15, "half_up").item() == 1


def test_shift_negative():
    x = np.array([-49152, 49152], dtype=np.int64)
    np.testing.assert_array_equal(
        shift_right_rounded(x, 15, "convergent"), [-2, 2]
    )
    np.testing.assert_array_equal(
        shift_right_rounded(x, 15, "truncate"), [-1, 1]
    )


def test_add_q_saturates():
    out, n = add_q(np.array([30000], dtype=np.int64), np.array([30000], dtype=np.int64), Q15)
    assert out.item() == 32767
    assert n == 1
    out, n = add_q(np.array([-30000], dtype=np.int64), np.array([-30000], dtype=np.int64), Q15)
    assert out.item() == -32768
    assert n == 1


def test_q31_product_uses_int64_headroom():
    # Two near-full-scale Q31 numbers: product ~2^62 fits exactly in int64.
    a = np.array([Q31.qmax], dtype=np.int64)
    m = mul_q(a, a, Q31, "truncate")
    # (1 - 2^-31)^2 in Q31 is just below qmax
    assert m.item() == Q31.qmax - 1


def test_floor_mode_quantizes_toward_minus_inf():
    q = QFormat(3, 1)
    assert to_fixed(0.4, q, "floor").fixed.item() == 0
    assert to_fixed(-0.4, q, "floor").fixed.item() == -1
    x = np.array([-3, -1, 1, 3], dtype=np.int64)
    np.testing.assert_array_equal(
        shift_right_rounded(x, 1, "floor"), [-2, -1, 0, 1]
    )


def test_wrap_int_twos_complement():
    q = QFormat(1, 3)  # range [-8, 7]
    assert wrap_int(8, q) == -8
    assert wrap_int(9, q) == -7
    assert wrap_int(-9, q) == 7
    assert wrap_int(5, q) == 5
    assert wrapped(8, q) and not wrapped(7, q)
