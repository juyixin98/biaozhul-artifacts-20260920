"""项目自定义定点常量。

全部数值均为本项目自定义的定点协议参数，不依赖任何外部链上实现。

价格语义
--------
``P = (sqrtP_X96 / 2**Q96) ** 2``，单位为 token1/token0
（1 单位 token0 值多少 token1），且 ``P = 1.0001 ** tick``。

- token0 -> token1 时，价格向下一个更小的 tick 边界移动（zero_for_one=True）。
- token1 -> token0 时，价格向下一个更大的 tick 边界移动（zero_for_one=False）。
"""

#: sqrtPrice 定点指数：sqrtPrice 以 Q96 定点数表示
Q96 = 96
Q96_SCALE = 1 << Q96

#: tick 底数 b = sqrt(1.0001)，价格每 tick 变化 1bp
TICK_SPACING_BASE_NUM = 10001
TICK_SPACING_BASE_DEN = 10000

#: 允许的整数 tick 范围（闭区间）
MIN_TICK = -887272
MAX_TICK = 887272

#: 费用单位：fee_ppm = 百万分之一（parts-per-million）
FEE_DENOMINATOR = 1_000_000

#: 流动性与数量均为整数（最小单位），不引入额外小数精度
AMOUNT_UNIT = 1

#: 报价主循环安全上限（区间数），防止异常输入造成无限循环
MAX_SWAP_STEPS = 1_000_000

#: 停止原因枚举
STOP_FILLED = "filled"                 # 输入全部成交
STOP_INCOMPLETE_GAP = "incomplete_gap"     # 前方出现空（零流动性）区间
STOP_INCOMPLETE_EDGE = "incomplete_edge"   # 已走到 MIN/MAX tick 网格尽头
STOP_INCOMPLETE_LIMIT = "incomplete_limit"  # 到达调用方指定的 limit tick
