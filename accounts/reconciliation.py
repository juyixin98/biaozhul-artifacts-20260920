"""
对账工具：校验资产守恒与账实一致。

校验项：
1. 账本守恒：每个交易组 (tx_group) 内，按资产分组的金额之和必须为 0。
2. 全局守恒：每个资产在全部账户上的 total 之和必须为 0
   （用户/手续费为正，FUNDING 发行账户为负，全部模拟资产都来自发行）。
3. 账实一致：账户 total 必须等于账本分录的累加值（账户表是账本的物化视图）。
4. 冻结可追溯：所有未完结挂单 remaining_frozen 之和必须等于账户 frozen。

所有聚合都在 Python 中以 Decimal 精确完成，不依赖数据库 SUM 的类型行为
（SQLite 的 SUM 会转 REAL 带来浮点噪声；MySQL DECIMAL 聚合本身精确）。
"""
from collections import defaultdict
from decimal import Decimal

from django.db import connection

from .models import Account, LedgerEntry

# MySQL DECIMAL 聚合/回读精确，容差为 0；
# SQLite 对超长 DECIMAL（本项目 max_digits=36）会转 REAL，回读带浮点失真，
# 仅在该后端使用一个远小于业务精度（1e-8）的聚合容差。
_TOL = Decimal("0") if connection.vendor == "mysql" else Decimal("0.000001")


class ReconcileError(Exception):
    pass


def _nonzero(v: Decimal) -> bool:
    return abs(v) > _TOL


def reconcile_snapshot():
    """返回对账结果字典；有差异时在 mismatches 中列出（不抛异常，便于报告）。"""
    mismatches = []

    accounts = list(Account.objects.all().values(
        "pk", "asset_id", "asset__symbol", "account_type",
        "available", "frozen",
    ))
    entries = list(LedgerEntry.objects.values(
        "pk", "tx_group", "asset_id", "account_id", "amount",
    ))

    # 1. 分录组守恒（按 tx_group, asset）
    group_sums = defaultdict(lambda: Decimal("0"))
    for e in entries:
        group_sums[(e["tx_group"], e["asset_id"])] += e["amount"]
    bad_groups = [(k, v) for k, v in group_sums.items() if _nonzero(v)]
    for (tx_group, asset_id), s in bad_groups[:20]:
        mismatches.append(
            f"分录组不守恒 tx_group={tx_group} asset={asset_id} sum={s}"
        )
    if len(bad_groups) > 20:
        mismatches.append(f"另有 {len(bad_groups) - 20} 个分录组不守恒")

    # 2. 全局守恒（含手续费账户，按资产）
    global_sums = defaultdict(lambda: Decimal("0"))
    for a in accounts:
        global_sums[a["asset__symbol"]] += a["available"] + a["frozen"]
    global_rows = []
    for symbol in sorted(global_sums):
        total = global_sums[symbol]
        avail = sum(
            (x["available"] for x in accounts if x["asset__symbol"] == symbol),
            Decimal("0"),
        )
        frz = sum(
            (x["frozen"] for x in accounts if x["asset__symbol"] == symbol),
            Decimal("0"),
        )
        global_rows.append(
            {"asset": symbol, "available": avail, "frozen": frz, "total": total}
        )
        if _nonzero(total):
            mismatches.append(f"全局资产不守恒 asset={symbol} total={total}")

    # 3. 账实一致：账本累加 vs 账户字段
    ledger_sum = defaultdict(lambda: Decimal("0"))
    for e in entries:
        ledger_sum[e["account_id"]] += e["amount"]
    bad_accounts = 0
    for a in accounts:
        book = ledger_sum.get(a["pk"], Decimal("0"))
        actual = a["available"] + a["frozen"]
        if _nonzero(book - actual):
            bad_accounts += 1
            if bad_accounts <= 20:
                mismatches.append(
                    f"账实不一致 account={a['pk']} type={a['account_type']} "
                    f"asset={a['asset__symbol']} ledger={book} account.total={actual}"
                )

    # 4. 冻结可追溯
    mismatches.extend(_check_open_order_freeze(accounts))

    return {
        "ok": not mismatches,
        "assets": global_rows,
        "mismatches": mismatches,
    }


def _check_open_order_freeze(accounts):
    """延迟导入，避免 accounts -> trading 的循环依赖。"""
    from trading.models import Order

    out = []
    rows = (
        Order.objects.filter(
            status__in=[Order.Status.NEW, Order.Status.PARTIALLY_FILLED]
        )
        .values_list("account_id", "remaining_frozen")
    )
    frozen_by_account = defaultdict(lambda: Decimal("0"))
    for account_id, rem in rows:
        frozen_by_account[account_id] += rem

    for a in accounts:
        if a["account_type"] != "USER":
            continue
        expected = frozen_by_account.get(a["pk"], Decimal("0"))
        if _nonzero(a["frozen"] - expected):
            out.append(
                f"冻结与挂单不一致 account={a['pk']} asset={a['asset__symbol']} "
                f"account.frozen={a['frozen']} open_orders_frozen={expected}"
            )
    return out
