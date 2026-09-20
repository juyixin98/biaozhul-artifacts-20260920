"""
对账命令：
  python manage.py reconcile

校验（任何一项失败即退出码 1，便于接入巡检）：
1. 每个复式分录组按资产求和为 0；
2. 每个资产在全部账户（含手续费账户）上的 total 之和为 0；
3. 账户表余额与账本累加值一致；
4. 用户账户 frozen 与未完结挂单 remaining_frozen 之和一致。
"""
from django.core.management.base import BaseCommand

from accounts.reconciliation import reconcile_snapshot


class Command(BaseCommand):
    help = "运行资产守恒与账实一致对账检查"

    def handle(self, *args, **options):
        result = reconcile_snapshot()

        self.stdout.write("按资产汇总（available/frozen/total）：")
        for row in result["assets"]:
            self.stdout.write(
                f"  {row['asset']:8s} available={row['available']} "
                f"frozen={row['frozen']} total={row['total']}"
            )

        if result["ok"]:
            self.stdout.write(self.style.SUCCESS("对账通过：全部资产守恒、账实一致"))
            return

        self.stdout.write(self.style.ERROR(f"对账失败，共 {len(result['mismatches'])} 项差异："))
        for item in result["mismatches"]:
            self.stdout.write(self.style.ERROR(f"  - {item}"))
        raise SystemExit(1)
