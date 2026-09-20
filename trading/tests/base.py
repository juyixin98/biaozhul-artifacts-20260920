"""测试公共夹具。"""
from decimal import Decimal as D

from django.contrib.auth.models import User
from django.test import TestCase

from accounts.models import Asset
from accounts.reconciliation import reconcile_snapshot
from accounts.services import issue_simulated_asset
from trading.engine import bootstrap_order_book, engine
from trading.models import SystemConfig, TradingPair

FUND = {
    "USDT": D("100000000"),
    "BTC": D("100000"),
    "ETH": D("100000"),
}


class EngineTestMixin:
    """共用初始化：资产/交易对/用户/充值/重置内存簿。"""

    def setUp(self):
        engine.reset_for_tests()
        self.usdt, _ = Asset.objects.get_or_create(symbol="USDT")
        self.btc, _ = Asset.objects.get_or_create(symbol="BTC")
        self.eth, _ = Asset.objects.get_or_create(symbol="ETH")
        self.pair, _ = TradingPair.objects.get_or_create(
            symbol="BTCUSDT",
            defaults={
                "base": self.btc,
                "quote": self.usdt,
                "tick_size": D("0.01"),
                "lot_size": D("0.000001"),
                "min_notional": D("0"),
            },
        )
        self.alice = User.objects.create_user("alice", password="x")
        self.bob = User.objects.create_user("bob", password="x")
        self.carol = User.objects.create_user("carol", password="x")
        for u in (self.alice, self.bob, self.carol):
            for asset in (self.usdt, self.btc, self.eth):
                issue_simulated_asset(u, asset, FUND[asset.symbol])
        SystemConfig.load()  # 默认 0.001 / 0.001，非维护
        bootstrap_order_book()

    def bal(self, user, symbol):
        acc = AccountSafe(user, symbol)
        return acc

    def account(self, user, asset):
        from accounts.models import Account

        return Account.objects.get(user=user, asset=asset)


def AccountSafe(user, symbol):
    from accounts.models import Account

    return Account.objects.get(user=user, asset__symbol=symbol)


class BaseEngineTestCase(EngineTestMixin, TestCase):
    def assertConserved(self):
        result = reconcile_snapshot()
        self.assertTrue(result["ok"], result["mismatches"])
