"""定点解析与乘除方向测试。"""
from __future__ import annotations

import pytest

from app.numerics import (
    WAD,
    fmt_amount,
    mul_div_ceil,
    mul_div_floor,
    parse_amount,
)


def test_parse_integer_and_decimal_string():
    assert parse_amount("100") == 100 * WAD
    assert parse_amount("0.1") == WAD // 10
    assert parse_amount("1.000000000000000001") == WAD + 1
    assert parse_amount(0) == 0


def test_parse_rejects_float_negative_and_too_fine():
    with pytest.raises(ValueError):
        parse_amount(1.5)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        parse_amount("-1")
    with pytest.raises(ValueError):
        parse_amount("0.0000000000000000001")  # 19 位小数


def test_floor_and_ceil_rounding():
    # 1e18 / 3：floor 与 ceil 相差 1，方向严格
    assert mul_div_floor(WAD, 1, 3) == WAD // 3
    assert mul_div_ceil(WAD, 1, 3) == WAD // 3 + 1
    # floor 不会凭空增发：极小利息单笔归零
    assert mul_div_floor(1, 1, 3) == 0


def test_fmt_roundtrip():
    assert fmt_amount(parse_amount("123.456")) == "123.456"
    assert fmt_amount(0) == "0"
    assert fmt_amount(WAD + 1) == "1.000000000000000001"
