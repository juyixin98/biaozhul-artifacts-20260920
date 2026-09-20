"""
初始化模拟资产（幂等：资产/交易对用 get_or_create；用户资产仅在不足时补足）。

创建：
- 资产：USDT、BTC、ETH
- 交易对：BTC/USDT（tick 0.01, lot 0.000001）、ETH/USDT（tick 0.01, lot 0.0001）
- 用户：admin（管理员）、alice、bob（均为弱口令，仅供本地模拟）
- 模拟发行：每个普通用户 1,000,000 USDT / 10 BTC / 100 ETH
- 系统配置单行（taker/maker 默认 0.1%，非维护模式）

发行规则见 accounts.services.issue_simulated_asset：
用户 +X，FUNDING 发行账户 -X，全局加总恒为 0。
"""
from decimal import Decimal

from django.contrib.auth.models import User
from django.core.management.base import BaseCommand
from django.db import transaction

from accounts.models import Asset, Account
from accounts.services import issue_simulated_asset
from trading.models import SystemConfig, TradingPair

SEED = {
    "USDT": Decimal("1000000"),
    "BTC": Decimal("10"),
    "ETH": Decimal("100"),
}

PAIRS = [
    # symbol, base, quote, tick, lot, min_notional
    ("BTCUSDT", "BTC", "USDT", "0.01", "0.000001", "10"),
    ("ETHUSDT", "ETH", "USDT", "0.01", "0.0001", "5"),
]


class Command(BaseCommand):
    help = "初始化模拟资产、交易对与演示用户（幂等，可重复执行）"

    def add_arguments(self, parser):
        parser.add_argument(
            "--reset-users",
            action="store_true",
            help="重置演示用户口令（不影响余额）",
        )

    def handle(self, *args, **options):
        with transaction.atomic():
            assets = {}
            for symbol in ("USDT", "BTC", "ETH"):
                assets[symbol], _ = Asset.objects.get_or_create(
                    symbol=symbol, defaults={"name": symbol}
                )

            for symbol, base, quote, tick, lot, min_notional in PAIRS:
                TradingPair.objects.get_or_create(
                    symbol=symbol,
                    defaults={
                        "base": assets[base],
                        "quote": assets[quote],
                        "tick_size": tick,
                        "lot_size": lot,
                        "min_notional": min_notional,
                    },
                )

            SystemConfig.load()

            admin, created = User.objects.get_or_create(
                username="admin", defaults={"is_staff": True, "is_superuser": True}
            )
            if created or options["reset_users"]:
                admin.set_password("admin123")
                admin.is_staff = True
                admin.is_superuser = True
                admin.save()

            for name in ("alice", "bob"):
                user, created = User.objects.get_or_create(username=name)
                if created or options["reset_users"]:
                    user.set_password("trade123")
                    user.save()
                self._fund(user, assets)

        self.stdout.write(self.style.SUCCESS(
            "模拟数据就绪：admin/admin123（管理员），alice/trade123，bob/trade123"
        ))

    def _fund(self, user, assets: dict) -> None:
        """每个资产只在用户当前 total 低于目标时补发差额（幂等补足）。"""
        for symbol, target in SEED.items():
            asset = assets[symbol]
            acc = Account.objects.filter(user=user, asset=asset).first()
            current = acc.total if acc else Decimal("0")
            if current < target:
                gap = target - current
                with transaction.atomic():
                    issue_simulated_asset(user, asset, gap)
                self.stdout.write(f"  {user.username} 补发 {gap} {symbol}")
