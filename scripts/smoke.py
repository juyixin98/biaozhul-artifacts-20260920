"""快速冒烟：限价/市价/部分成交/撤单/对账（SQLite 单线程即可验证业务逻辑）。"""
import os

import django

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "cryptolaunch.settings")
django.setup()

from decimal import Decimal as D  # noqa: E402

from django.contrib.auth.models import User  # noqa: E402

from accounts.models import Account, Asset  # noqa: E402
from accounts.reconciliation import reconcile_snapshot  # noqa: E402
from accounts.services import issue_simulated_asset  # noqa: E402
from trading.engine import bootstrap_order_book, engine  # noqa: E402
from trading.models import Order, TradingPair  # noqa: E402

bootstrap_order_book()

pair = TradingPair.objects.get(symbol="BTCUSDT")
base, quote = pair.base, pair.quote
alice = User.objects.get(username="alice")
bob = User.objects.get(username="bob")


def bal(user, asset):
    acc = Account.objects.get(user=user, asset=asset)
    return acc.available, acc.frozen


# alice 卖 2 BTC @ 50000
o1, c1 = engine.submit_order(user=alice, pair=pair, side="SELL",
                             order_type="LIMIT", quantity=D("2"),
                             limit_price=D("50000"), idem_key="a-sell-1")
print("alice sell:", o1.status, c1, "frozen", bal(alice, base))

# bob 买 1 BTC @ 50000 -> 部分成交 maker 单
o2, c2 = engine.submit_order(user=bob, pair=pair, side="BUY",
                             order_type="LIMIT", quantity=D("1"),
                             limit_price=D("50000"), idem_key="b-buy-1")
o1.refresh_from_db()
print("bob buy 1:", o2.status, "| alice sell now:", o1.status,
      "filled", o1.filled_qty, "rem_frozen", o1.remaining_frozen)
print("alice USDT:", bal(alice, quote), "bob BTC:", bal(bob, base),
      "bob USDT frozen:", bal(bob, quote))

# 幂等重复
_o2b, created2 = engine.submit_order(user=bob, pair=pair, side="BUY",
                                     order_type="LIMIT", quantity=D("1"),
                                     limit_price=D("50000"), idem_key="b-buy-1")
print("idempotent repeat returns same order:", _o2b.pk == o2.pk, "created:", created2)

# bob 市价买 0.5 BTC 预算 30000 USDT（吃 alice 剩余 1 BTC 的 0.5）
o3, _ = engine.submit_order(user=bob, pair=pair, side="BUY",
                            order_type="MARKET", quantity=D("30000"))
o1.refresh_from_db()
print("market buy:", o3.status, o3.filled_qty, "alice sell:", o1.status, o1.filled_qty)
print("bob USDT after market:", bal(bob, quote), "(剩余预算应已退回)")

# 撤掉 alice 剩余 1 BTC 卖单
cancelled = engine.cancel_order(user=alice, order_id=o1.pk)
print("cancel:", cancelled.status, "alice BTC:", bal(alice, base))
# 重复撤单安全
again = engine.cancel_order(user=alice, order_id=o1.pk)
print("repeat cancel idempotent:", again.status)

r = reconcile_snapshot()
print("RECONCILE:", r["ok"])
for m in r["mismatches"]:
    print("  MISMATCH:", m)
for row in r["assets"]:
    print("  asset", row)

# 订单簿快照
print("book:", engine.book(pair.pk).snapshot())
