"""经证明的十进制近似（拒绝假精度）。

输入是精确有理数孤立区间 [a, b]（端点非根，根 ξ ∈ (a,b)）或精确点根。
输出一个十进制中点与一个**严格成立**的十进制误差半径 R，满足
        |ξ - midpoint_decimal| <= radius_decimal
所有比较与舍入都在 Fraction 上完成；Decimal/浮点完全不参与判定。

构造（区间情形，h=b-a, r=h/2）：
  * 取量子化单位 u = 10^-d（d 为整数），选 u 为“不小于 r 的最大十进量子”，
    即 r <= u < 10r；
  * 中点按“四舍六入五成双”舍入到最近的 u 的整数倍 q·u，舍入误差 <= u/2；
  * 故总误差 <= r + u/2，半径按 u 为单位**向上取整**后输出，绝不向下乐观。
点根情形 r=0，按目标 epsilon 选最小的 u <= epsilon，半径恰为 u/2。
"""
from __future__ import annotations

from fractions import Fraction

from .limits import MAX_DECIMAL_DIGITS


class DecimalizationError(Exception):
    """所需小数位数超过硬上限时抛出（宁可不给数字，也不伪造精度）。"""


def _unit(d: int) -> Fraction:
    if d >= 0:
        return Fraction(1, 10 ** d)
    return Fraction(10 ** (-d))


def _floor_log10_int(T: int) -> int:
    """精确计算 floor(log10(T))，T >= 1；不使用浮点。

    先用 bit_length 给出足够接近的初值（误差不超过 1），再用 10 的整数幂
    精确校正，幂始终很小（与答案同量级），不会出现 10**巨大值。
    """
    d = (T.bit_length() - 1) * 30103 // 100000
    p = 10 ** d
    while p * 10 <= T:
        p *= 10
        d += 1
    while p > T:
        p //= 10
        d -= 1
    return d


def _digits_for_radius(r: Fraction) -> int:
    """最大的 u=10^-d >= r 所对应的 d。

    u>=r  <=>  10^d <= 1/r = den_r/num_r；取最大的这样的 d，
    即 d = floor(log10(1/r))。10^d 通常极小（d 多为正），运算廉价。
    """
    num, den = r.denominator, r.numerator
    d = _floor_log10_int(max(num // den, 1))
    while 10 ** (d + 1) * den <= num:
        d += 1
    while 10 ** d * den > num:
        d -= 1
    return d


def _digits_for_point(eps: Fraction) -> int:
    """点根：最小的 u=10^-d <= eps，对应 d = ceil(log10(1/eps))。"""
    num, den = eps.denominator, eps.numerator  # 1/eps
    # 最小 d 使 10^-d <= eps <=> 10^d >= num/den
    d = _floor_log10_int(max(num // den, 1))
    while 10 ** d * den < num:
        d += 1
    return max(0, d)


def _round_half_even(f: Fraction) -> int:
    n, den = f.numerator, f.denominator
    q, rem = divmod(abs(n), den)
    if rem * 2 > den:
        q += 1
    elif rem * 2 == den and q % 2 == 1:
        q += 1
    return q if n >= 0 else -q


def _format_fixed(q: int, d: int) -> str:
    """把整数 q（表示 q·10^-d）格式化为恰好 d 位小数的十进制字符串。"""
    neg = q < 0
    s = str(abs(q)).zfill(max(d, 0) + 1)
    if d <= 0:
        body = s + "0" * (-d)
        return ("-" if neg else "") + (body or "0")
    int_part, frac_part = s[:-d], s[-d:]
    return ("-" if neg else "") + int_part + "." + frac_part


def decimalize_interval(a: Fraction, b: Fraction) -> dict:
    r = (b - a) / 2
    mid = (a + b) / 2
    d = _digits_for_radius(r)
    if d > MAX_DECIMAL_DIGITS:
        raise DecimalizationError(
            f"需要 {d} 位小数，超过上限 {MAX_DECIMAL_DIGITS}")
    u = _unit(d)
    q = _round_half_even(mid / u)
    mid_dec = q * u
    err_bound = r + u / 2                      # 严格上界（Fraction）
    rad_units = _ceil_div_units(err_bound, u)  # ceil(err/u) 个量子
    return {
        "midpoint": mid_dec,
        "midpoint_decimal": _format_fixed(q, d),
        "radius_decimal": _format_fixed(rad_units, d),
        "decimal_places": d,
        "radius_exact": rad_units * u,
        "error_bound_note": "|root - midpoint_decimal| <= radius_decimal",
    }


def _ceil_div_units(x: Fraction, u: Fraction) -> int:
    """ceil(x / u)，x,u > 0，精确整数运算。"""
    z = x / u
    return -((-z.numerator) // z.denominator)


def decimalize_point(r: Fraction, eps: Fraction) -> dict:
    d = _digits_for_point(eps)
    if d > MAX_DECIMAL_DIGITS:
        raise DecimalizationError(
            f"需要 {d} 位小数，超过上限 {MAX_DECIMAL_DIGITS}")
    u = _unit(d)
    q = _round_half_even(r / u)
    r_dec = q * u
    err_bound = u / 2          # 点根本身精确，误差只来自十进制舍入
    rad_units = _ceil_div_units(err_bound, u)
    return {
        "midpoint": r_dec,
        "midpoint_decimal": _format_fixed(q, d),
        "radius_decimal": _format_fixed(rad_units, d),
        "decimal_places": d,
        "radius_exact": rad_units * u,
        "error_bound_note": "exact rational root; |root - midpoint_decimal| "
                            "<= radius_decimal",
    }
