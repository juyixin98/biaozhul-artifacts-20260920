"""整数定点数学。

所有金额、价格、利率在内部都以 WAD = 10^18 的整数表示（18 位小数）。
- 输入侧按字符串逐位精确解析十进制数，禁止浮点进入计算。
- 乘除使用带方向的整除：利息/费用一律向零截断（floor，因金额非负），
  避免"系统凭空多出 1 wei"。清算没收阶段需要向上取整保证足额的地方单独标注。
"""
from __future__ import annotations

from decimal import Decimal
from typing import Union

WAD = 10**18

# 输入允许的类型
AmountInput = Union[str, int, Decimal]


class FixedPointError(ValueError):
    """定点解析/运算错误（例如超过 18 位小数、负数）。"""


def _parse_decimal_str(s: str, field: str) -> int:
    """把十进制字符串精确转换为 WAD 整数（非负数）。"""
    s = s.strip()
    if s.startswith("-"):
        raise FixedPointError("不允许负数")
    if s.startswith("+"):
        s = s[1:]
    if not s or s.lower() in ("nan", "inf", "infinity"):
        raise FixedPointError("空字符串或非有限数值")

    if "." in s:
        whole, frac = s.split(".", 1)
        if not whole:
            whole = "0"
        if not frac.isdigit() or not whole.isdigit():
            raise FixedPointError(f"非法十进制数 {s!r}")
        if len(frac) > 18:
            raise FixedPointError(f"小数位超过 18 位（值={s!r}）")
        return int(whole) * WAD + int(frac.ljust(18, "0"))

    if not s.isdigit():
        raise FixedPointError(f"非法十进制数 {s!r}")
    return int(s) * WAD


def parse_amount(value: AmountInput, *, field: str = "amount") -> int:
    """把十进制字符串或整数转为 WAD 整数。

    - 接受 "1.5"、"100"、Decimal、int。
    - 拒绝 float（调用方应先转 str）。
    - 小数位超过 18 位直接报错，不做静默截断。
    - 拒绝负数（本协议金额均为非负）。
    """
    try:
        if isinstance(value, bool):
            raise FixedPointError("布尔值不是合法金额")
        if isinstance(value, float):
            raise FixedPointError("禁止使用 float，请用字符串传递精确小数")
        if isinstance(value, int):
            if value < 0:
                raise FixedPointError("不允许负数")
            return value * WAD
        if isinstance(value, Decimal):
            if not value.is_finite():
                raise FixedPointError("数值必须有限")
            return _parse_decimal_str(format(value, "f"), field)
        if isinstance(value, str):
            return _parse_decimal_str(value, field)
        raise FixedPointError(f"不支持的类型 {type(value)!r}")
    except FixedPointError as exc:
        raise FixedPointError(f"{field}: {exc}") from None


def fmt_amount(wad: int) -> str:
    """WAD 整数 -> 规范十进制字符串（输出给人类/JSON 时使用）。"""
    if wad < 0:
        sign = "-"
        wad = -wad
    else:
        sign = ""
    whole, frac = divmod(wad, WAD)
    if frac == 0:
        return f"{sign}{whole}"
    return f"{sign}{whole}.{str(frac).zfill(18).rstrip('0')}"


def fmt_ratio(numerator: int, denominator: int, places: int = 6) -> str:
    """以定点安全的方式输出比值（仅用于展示，不参与协议计算）。"""
    if denominator == 0:
        return "0"
    scale = 10**places
    q, r = divmod(numerator, denominator)
    return f"{q}.{str(r * scale // denominator).zfill(places)}"


def mul_div_floor(x: int, y: int, z: int) -> int:
    """floor(x * y / z)，中间乘积用 Python 大整数，不溢出。金额非负 => floor 即向零截断。"""
    assert x >= 0 and y >= 0 and z > 0
    return (x * y) // z


def mul_div_ceil(x: int, y: int, z: int) -> int:
    """ceil(x * y / z)，用于清算时保证没收抵押品的名义价值不少于应得。"""
    assert x >= 0 and y >= 0 and z > 0
    return (x * y + z - 1) // z
