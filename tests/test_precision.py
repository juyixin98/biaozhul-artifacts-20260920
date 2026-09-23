"""Tests for normalization and final integer rounding rules."""

from __future__ import annotations

from decimal import Decimal as D

import pytest

from app.core import config
from app.core.precision import (
    high_precision,
    relative_error,
    scale_factor,
    to_normalized,
    to_units_floor,
)


def test_roundtrip_identity(hp):
    for units, dec_ in [(1, 6), (123456789, 18), (10 ** 30, 6), (7, 0)]:
        assert to_units_floor(to_normalized(units, dec_), dec_) == units


def test_output_rounding_is_floor(hp):
    # 1.9999999 of a 6-decimal token must floor to 1999999 units, not round up.
    assert to_units_floor(D("1.9999999"), 6) == 1999999
    assert to_units_floor(D("0.9999999"), 6) == 999999
    assert to_units_floor(D("123.0000009"), 6) == 123000000
    # exact boundary stays exact
    assert to_units_floor(D("2.000000"), 6) == 2000000


def test_floor_never_exceeds_real_value(hp):
    # floor(x*10^d) <= x*10^d for any positive x
    for s in ["0.0000001", "1.0000009", "99.9999999", "0.000000000000000001"]:
        x = D(s)
        for dec_ in [0, 6, 18]:
            assert to_units_floor(x, dec_) <= x * scale_factor(dec_)


def test_negative_amount_rejected(hp):
    with pytest.raises(ValueError):
        to_normalized(-1, 6)
    with pytest.raises(ValueError):
        to_units_floor(D("-0.5"), 6)


def test_relative_error_is_scale_safe(hp):
    assert relative_error(D("100"), D("100")) == 0
    # |101-100| / max(101,100) = 1/101
    assert relative_error(D("101"), D("100")) == D(1) / D(101)
    # zero vs zero -> 0, zero vs nonzero -> 1 (denominator floored at 1)
    assert relative_error(D("0"), D("0")) == 0
    assert relative_error(D("0"), D("1")) == 1


def test_context_isolation():
    # high_precision must not leak into the ambient Decimal context
    import decimal

    before = decimal.getcontext().prec
    with high_precision():
        assert decimal.getcontext().prec == config.DECIMAL_PREC
    assert decimal.getcontext().prec == before
