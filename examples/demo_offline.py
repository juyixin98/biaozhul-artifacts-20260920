"""离线演示：不启动 HTTP 服务，直接调用引擎并打印逐段证据。

运行：python examples/demo_offline.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.clmm.engine import Pool, Position, quote_swap  # noqa: E402
from app.clmm.pricing import tick_sqrt_x96  # noqa: E402


def show(title, pool, zfo, amount, limit_tick=None):
    print("=" * 78)
    print(title)
    r = quote_swap(pool, zfo, amount, limit_tick)
    d = r.to_dict()
    print(
        f"方向={d['direction']}  输入={d['amount_in']}  "
        f"输出={d['amount_out_total']}  费用={d['fee_total']}  "
        f"未成交={d['amount_in_unfilled']}"
    )
    print(
        f"停止原因={d['stop_reason']}  stop_tick={d['stop_tick']}  "
        f"tick {d['tick_start']} -> {d['tick_end']}  对账={d['input_reconciled']}"
    )
    for s in d["segments"]:
        print(
            f"  段{s['segment_index']} tick[{s['tick_lo']},{s['tick_hi']}) "
            f"L={s['liquidity']} 毛入={s['amount_in_gross']} "
            f"费={s['fee']} 本金={s['principal_in']} 出={s['amount_out']} "
            f"结束={s['ended_by']}"
        )
        print(
            f"      sqrtP {s['sqrt_price_start_x96']} -> {s['sqrt_price_end_x96']}"
        )
    return r


def main():
    # 三区间阶梯流动性：[-100,0) L=1e18, [0,100) L=3e18, [100,200) L=9e18
    pool = Pool(
        pool_id="demo",
        token0="USDC",
        token1="WETH",
        fee_ppm=3000,
        sqrt_price_x96=tick_sqrt_x96(0),
        positions=(
            Position(-100, 0, 10**18),
            Position(0, 100, 3 * 10**18),
            Position(100, 200, 9 * 10**18),
        ),
    )

    show("1) 小额上行（段内成交）", pool, False, 10**12)
    show("2) 大额上行（跨 tick=100 边界，多段扣费）", pool, False, 10**24)
    show("3) 下行方向同样多段", pool, True, 10**24)
    show("4) 带 limit_tick 的报价", pool, False, 10**30, limit_tick=50)

    # 空区间：当前 tick=0 处两个仓位都不覆盖（[-100,-1) 与 [1,100)）
    gap = Pool(
        pool_id="gap",
        token0="A",
        token1="B",
        fee_ppm=3000,
        sqrt_price_x96=tick_sqrt_x96(0),
        positions=(Position(-100, -1, 10**12), Position(1, 100, 10**12)),
    )
    r = show("5) 前方即空区间：立即停止，输入原样返回，不计费", gap, False, 10**18)
    assert r.amount_in_unfilled == 10**18 and r.fee_total == 0

    # 极小流动性 L=1 与最大输入
    tiny = Pool(
        pool_id="tiny", token0="A", token1="B", fee_ppm=3000,
        sqrt_price_x96=tick_sqrt_x96(0), positions=(Position(-3, 3, 1),),
    )
    show("6) 极小流动性 L=1，超大输入（无除零，逐段对账）", tiny, True, 10**40)

    print("=" * 78)
    print("全部演示完成。")


if __name__ == "__main__":
    main()
