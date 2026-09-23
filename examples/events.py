"""示例输入: 一段离线抵押借贷事件序列 (全部金额为 1e18 wei 整数)。

场景时间线 (秒相对基准 T0):
  t=0     分段利率年化 10% (自 T0 生效)
  t=0     ORACLE: ETH = 2000
  t=0     OPEN 仓位 A: 抵押 10 ETH, 债务 10000, 清算阈值 0.8
  t=0     OPEN 仓位 B: 抵押 0.001 ETH, 债务 10 (用于演示抵押不足部分成交)
  t=3600  分段利率切换为年化 20% (验收: 利率切换边界)
  t=3600  ORACLE 维持 2000
  t=7200  ORACLE: ETH 跌到 900 (A/B 健康度均 < 1)
  t=7201  LIQUIDATE A: 偿还 2000 (成功, 8% 奖励, 没收 2.4 ETH)
  t=7202  LIQUIDATE A: 再偿还 2000 (成功 -> 验收重复/多次清算)
  t=7203  LIQUIDATE B: 请求偿还 5; 抵押不足 -> 按公式部分成交
  t=7300  ORACLE: ETH = 900 (最新价格)
  t=99999 LIQUIDATE: 价格年龄 >> 60 秒 -> STALE_PRICE 被拒
"""
from __future__ import annotations

T0 = 1_700_000_000
ETH = "ETH"
COLLATERAL_10ETH = 10 * 10**18
COLLATERAL_DUST = 10**15  # 0.001 ETH
DEBT_10000 = 10_000 * 10**18
DEBT_10 = 10 * 10**18
THRESHOLD_80PCT = 8 * 10**17  # 0.8 WAD
PRICE_2000 = 2_000 * 10**18
PRICE_900 = 900 * 10**18
ANNUAL_10PCT = 10**17  # 0.1 WAD
ANNUAL_20PCT = 2 * 10**17  # 0.2 WAD
REPAY_2000 = 2_000 * 10**18
REPAY_5 = 5 * 10**18


def example_events() -> list[dict]:
    return [
        {
            "event_id": "ev-rate-10",
            "ts": T0,
            "seq": 0,
            "action": "RATE_SCHEDULE",
            "payload": {"effective_ts": T0, "annual_rate_wad": ANNUAL_10PCT},
        },
        {
            "event_id": "ev-price-2000",
            "ts": T0,
            "seq": 1,
            "action": "ORACLE",
            "payload": {"asset": ETH, "price": PRICE_2000},
        },
        {
            "event_id": "ev-open-a",
            "ts": T0,
            "seq": 2,
            "action": "OPEN",
            "payload": {
                "position_id": "A",
                "asset": ETH,
                "collateral": COLLATERAL_10ETH,
                "debt": DEBT_10000,
                "liquidation_threshold": THRESHOLD_80PCT,
            },
        },
        {
            "event_id": "ev-open-b",
            "ts": T0,
            "seq": 3,
            "action": "OPEN",
            "payload": {
                "position_id": "B",
                "asset": ETH,
                "collateral": COLLATERAL_DUST,
                "debt": DEBT_10,
                "liquidation_threshold": THRESHOLD_80PCT,
            },
        },
        {
            "event_id": "ev-rate-20",
            "ts": T0 + 3600,
            "seq": 0,
            "action": "RATE_SCHEDULE",
            "payload": {
                "effective_ts": T0 + 3600,
                "annual_rate_wad": ANNUAL_20PCT,
            },
        },
        {
            "event_id": "ev-price-2000-b",
            "ts": T0 + 3600,
            "seq": 1,
            "action": "ORACLE",
            "payload": {"asset": ETH, "price": PRICE_2000},
        },
        {
            "event_id": "ev-price-900",
            "ts": T0 + 7200,
            "seq": 0,
            "action": "ORACLE",
            "payload": {"asset": ETH, "price": PRICE_900},
        },
        {
            "event_id": "ev-liq-a-1",
            "ts": T0 + 7201,
            "seq": 0,
            "action": "LIQUIDATE",
            "payload": {"position_id": "A", "repay": REPAY_2000},
        },
        {
            "event_id": "ev-liq-a-2",
            "ts": T0 + 7202,
            "seq": 0,
            "action": "LIQUIDATE",
            "payload": {"position_id": "A", "repay": REPAY_2000},
        },
        {
            "event_id": "ev-liq-b-partial",
            "ts": T0 + 7203,
            "seq": 0,
            "action": "LIQUIDATE",
            "payload": {"position_id": "B", "repay": REPAY_5},
        },
        {
            "event_id": "ev-price-900-b",
            "ts": T0 + 7300,
            "seq": 0,
            "action": "ORACLE",
            "payload": {"asset": ETH, "price": PRICE_900},
        },
        {
            "event_id": "ev-liq-stale",
            "ts": T0 + 99_999,
            "seq": 0,
            "action": "LIQUIDATE",
            "payload": {"position_id": "A", "repay": 10**18},
        },
    ]
