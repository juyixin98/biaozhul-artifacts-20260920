"""
定点数与舍入规则（全系统统一入口）。

规则约定：
- 所有金额/数量/价格都使用 decimal.Decimal，禁止 float。
- 统一精度 SCALE = 8 位小数（与交易所常见约定一致）。
- 价格、数量、金额的量化结果一律保留 8 位小数：
    * 成交金额 amount = price * qty 使用 ROUND_HALF_UP（四舍五入，5 入）。
    * 用户提交的价格/数量使用 ROUND_HALF_UP 量化到交易对的 tick/lot，
      再校验与原值一致（不允许隐式改变用户价格）。
- 冻结（freeze）使用 ROUND_CEILING（向上取整）：
    * 宁可多冻结用户资产，也不能出现冻结不足导致成交后负余额；
      订单结束时多冻结的零头（dust）退回可用余额。
- 手续费使用 ROUND_FLOOR（对正数向下取整）：
    * 手续费最少为 0，向下取整保证平台扣费不会超过规则应收，
      也不会因为舍入导致用户“到手数量”为负；小于 1e-8 的费用按 0 处理。
"""
from decimal import ROUND_CEILING, ROUND_FLOOR, ROUND_HALF_UP, Decimal, localcontext

SCALE = 8
QUANT = Decimal("1e-8")
ZERO = Decimal("0")


def D(value) -> Decimal:
    """把字符串/整数安全转为 Decimal（禁止从 float 构造）。"""
    if isinstance(value, Decimal):
        return value
    if isinstance(value, float):
        raise TypeError("禁止使用 float 构造金额，请使用字符串")
    return Decimal(str(value))


def quantize(value: Decimal, quantum: Decimal = QUANT, rounding=ROUND_HALF_UP) -> Decimal:
    """按指定刻度与舍入模式量化。"""
    with localcontext() as ctx:
        ctx.prec = 50
        return value.quantize(quantum, rounding=rounding)


def round_price(value: Decimal, tick: Decimal) -> Decimal:
    """成交价：8 位刻度，HALF_UP。"""
    return quantize(value, QUANT, ROUND_HALF_UP)


def round_amount(value: Decimal) -> Decimal:
    """成交金额：8 位刻度，HALF_UP。"""
    return quantize(value, QUANT, ROUND_HALF_UP)


def round_qty(value: Decimal, lot: Decimal) -> Decimal:
    """成交数量按 lot 刻度截断（撮合循环按 lot 整数倍推进，这里做防御）。"""
    return quantize(value, lot, ROUND_HALF_UP)


def ceil8(value: Decimal) -> Decimal:
    """冻结金额：向上取整到 1e-8。"""
    return quantize(value, QUANT, ROUND_CEILING)


def floor8(value: Decimal) -> Decimal:
    """手续费：向下取整到 1e-8（小于最小刻度则为 0）。"""
    return quantize(value, QUANT, ROUND_FLOOR)


def apply_tick(value: Decimal, tick: Decimal) -> Decimal:
    """
    校验并量化用户价格：必须是 tick 的整数倍（在 8 位精度内）。
    不满足抛 ValueError，由上层返回 400。
    """
    with localcontext() as ctx:
        ctx.prec = 50
        ratio = value / tick
        n = ratio.to_integral_value(rounding=ROUND_HALF_UP)
        if n * tick != value:
            raise ValueError("价格必须是最小报价单位的整数倍")
    return quantize(value, QUANT, ROUND_HALF_UP)


def apply_lot(value: Decimal, lot: Decimal) -> Decimal:
    """校验并量化用户数量：必须是 lot 的整数倍。"""
    with localcontext() as ctx:
        ctx.prec = 50
        ratio = value / lot
        n = ratio.to_integral_value(rounding=ROUND_HALF_UP)
        if n * lot != value:
            raise ValueError("数量必须是最小下单数量的整数倍")
    return quantize(value, QUANT, ROUND_HALF_UP)
