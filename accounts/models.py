"""
账户与双分录账本模型。

资产守恒模型：
- Account 是按“用户 × 资产”维度的余额账户；平台还为每种资产开设
  FEE（手续费归集）和 FUNDING（模拟资金发行）两类系统账户。
- 每一次真实的资产权属变化都写 LedgerEntry：一组 (tx_group, seq)
  构成一笔复式分录，组内所有分录金额之和必须为 0（有借必有贷，借贷必相等）。
- 下单时的冻结/释放只是 available <-> frozen 的内部划转，不改变总资产，
  因此不进账本（账本只记录 total 的权属变化）。
- 模拟资产初始化通过 FUNDING 账户发行：用户 +X，FUNDING -X，
  全系统按资产加总恒为 0，对账时 FEE 账户也纳入校验。

余额字段：
- available：可用余额
- frozen：下单冻结
- total = available + frozen（由服务层保证两者之和不出现负值）
"""
import uuid

from django.conf import settings
from django.db import models
from django.db.models import CheckConstraint, Q


class Asset(models.Model):
    """可交易/可持有的资产登记。"""

    symbol = models.CharField("资产符号", max_length=16, unique=True)
    name = models.CharField("名称", max_length=64, blank=True, default="")
    is_active = models.BooleanField("是否启用", default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "资产"
        verbose_name_plural = verbose_name

    def __str__(self):
        return self.symbol


class Account(models.Model):
    class AccountType(models.TextChoices):
        USER = "USER", "用户"
        FEE = "FEE", "手续费账户"
        FUNDING = "FUNDING", "资金发行账户"

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="accounts",
        verbose_name="所属用户(系统账户挂保留 SYSTEM 用户)",
    )
    asset = models.ForeignKey(Asset, on_delete=models.PROTECT, verbose_name="资产")
    account_type = models.CharField(
        "账户类型", max_length=16, choices=AccountType.choices
    )
    available = models.DecimalField("可用余额", max_digits=36, decimal_places=8, default=0)
    frozen = models.DecimalField("冻结余额", max_digits=36, decimal_places=8, default=0)

    class Meta:
        verbose_name = "账户"
        verbose_name_plural = verbose_name
        constraints = [
            # MySQL 不支持带条件的唯一约束，因此系统账户挂在保留 SYSTEM 用户下，
            # 用无条件复合唯一约束覆盖全部账户类型。
            models.UniqueConstraint(
                fields=["user", "asset", "account_type"],
                name="uniq_user_asset_type_account",
            ),
            # FUNDING 为模拟发行账户，其余额为负（=-已发行总量），属正常状态；
            # 只有用户账户与手续费账户禁止负余额。
            CheckConstraint(
                check=Q(account_type="FUNDING") | Q(available__gte=0),
                name="chk_available_nonneg",
            ),
            CheckConstraint(
                check=Q(account_type="FUNDING") | Q(frozen__gte=0),
                name="chk_frozen_nonneg",
            ),
        ]

    @property
    def total(self):
        return self.available + self.frozen

    def __str__(self):
        owner = self.user_id if self.user_id else self.account_type
        return f"{owner}:{self.asset.symbol}"


class LedgerEntry(models.Model):
    """
    追加式双分录流水。只允许 INSERT，不允许 UPDATE/DELETE（服务层约束）。

    amount 为对该账户 total 的带符号影响：正=资产增加，负=资产减少。
    同一 tx_group 内 amount 之和恒为 0。
    """

    tx_group = models.UUIDField("交易组", default=uuid.uuid4, db_index=True)
    seq = models.PositiveIntegerField("组内序号")
    account = models.ForeignKey(
        Account, on_delete=models.PROTECT, related_name="entries", verbose_name="账户"
    )
    asset = models.ForeignKey(Asset, on_delete=models.PROTECT, verbose_name="资产")
    amount = models.DecimalField("带符号金额", max_digits=36, decimal_places=8)
    memo = models.CharField("摘要", max_length=64)
    ref_type = models.CharField("关联类型", max_length=32)  # TRADE / DEPOSIT
    ref_id = models.CharField("关联ID", max_length=64)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        verbose_name = "账本分录"
        verbose_name_plural = verbose_name
        constraints = [
            models.UniqueConstraint(
                fields=["tx_group", "seq"], name="uniq_ledger_group_seq"
            ),
            CheckConstraint(check=~Q(amount=0), name="chk_ledger_amount_nonzero"),
        ]
        indexes = [
            models.Index(fields=["account", "created_at"]),
            models.Index(fields=["ref_type", "ref_id"]),
        ]

    def __str__(self):
        return f"{self.tx_group}#{self.seq} {self.amount} {self.asset_id}"
