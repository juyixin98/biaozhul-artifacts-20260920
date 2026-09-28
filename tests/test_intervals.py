"""Interval layer: outward rounding and the zero-division policy."""

import pytest
from mpmath import mp, mpf

from interval_cert.errors import DomainEvaluationError
from interval_cert.intervals import (
    contains_zero,
    int_pow,
    intersection,
    is_inside,
    make_interval,
    midpoint,
    point_interval,
    safe_divide,
    width,
)


def test_make_interval_rejects_empty():
    with pytest.raises(ValueError):
        make_interval(2, 1)


def test_outward_rounding_contains_true_value():
    # 1/3 is not representable; the interval must bracket the true value.
    with mp.workdps(80):
        true_third = mpf(1) / 3
    with mp.workdps(30):
        third = point_interval(1) / point_interval(3)
        assert third.a < true_third < third.b
        assert width(third) > 0  # outward rounded, not degenerate


def test_int_pow_even_negative_base():
    x = make_interval(-1, 2)
    assert int_pow(x, 2).a == 0 and int_pow(x, 2).b == 4
    assert int_pow(x, 3).a == -1 and int_pow(x, 3).b == 8


def test_safe_divide_refuses_zero_straddling_divisor():
    with pytest.raises(DomainEvaluationError) as excinfo:
        safe_divide(point_interval(1), make_interval(-1, 2), location="test/div")
    assert excinfo.value.category == "domain_error"
    assert excinfo.value.details["location"] == "test/div"
    assert excinfo.value.details["reason"] == "divisor_straddles_zero"


def test_safe_divide_normal_case():
    result = safe_divide(point_interval(1), make_interval(2, 4), location="t")
    assert result.a <= mpf("0.25") and result.b >= mpf("0.5")


def test_containment_and_intersection():
    outer = make_interval(0, 2)
    inner = make_interval(mpf("0.5"), mpf("1.5"))
    assert is_inside(inner, outer)
    assert not is_inside(outer, inner)
    assert intersection(make_interval(0, 1), make_interval(2, 3)) is None
    assert midpoint(make_interval(0, 2)) == 1
    assert contains_zero(make_interval(-1, 1))
    assert not contains_zero(make_interval(1, 2))
