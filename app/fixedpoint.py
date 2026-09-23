"""整数定点数工具。

约定: 所有金额(债务/抵押)、价格、利率因子均为带 1e18 缩放的整数 ("wei"),
即 1 个单位 = ``WAD = 10**18`` wei。全链路不使用浮点, 所有除法向下取整,
取整方向在协议中明确 (利息向下取整, 对协议更安全且确定性可复现)。
"""
from __future__ import annotations

from decimal import Decimal, localcontext

WAD = 10**18


def wmul(a: int, b: int) -> int:
    """WAD 定点乘法, 结果向下取整。"""
    return (a * b) // WAD


def wdiv(a: int, b: int) -> int:
    """WAD 定点除法, 结果向下取整; b 必须非零。"""
    if b == 0:
        raise ZeroDivisionError("wdiv by zero")
    return (a * WAD) // b


def wad_int(n: int) -> int:
    """普通整数 (如代币个数) -> WAD 整数。"""
    return n * WAD


def parse_wad(text: str | int) -> int:
    """把十进制字符串 (如 "12.5") 精确解析为 WAD 整数, 不经过 float。"""
    with localcontext() as ctx:
        ctx.prec = 80
        d = Decimal(str(text)) * Decimal(WAD)
    return int(d.to_integral_value(rounding="ROUND_FLOOR"))


def fmt_wad(x: int) -> str:
    """WAD 整数 -> 规范十进制字符串 (示例/调试用, 不参与协议计算)。"""
    sign = "-" if x < 0 else ""
    x = abs(x)
    whole, frac = divmod(x, WAD)
    if frac == 0:
        return f"{sign}{whole}"
    return f"{sign}{whole}.{str(frac).zfill(18).rstrip('0')}"
