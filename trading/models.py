"""
交易领域模型：交易对、订单、成交、幂等键、系统配置、审计日志。

订单状态机：
  NEW              新挂单（全部数量未成交，在簿）
  PARTIALLY_FILLED 部分成交（剩余数量在簿）
  FILLED           全部成交（终态）
  CANCELED         已撤单（终态；市价单吃不完也会进入此态，剩余取消）

撮合规则：价格优先、同价时间优先（id 单调递增代表时间先后），可部分成交。
"""
from django.conf import settings
from django.db import models
from django.db.models import CheckConstraint, Q

from accounts.models import Account, Asset


class TradingPair(models.Model):
    """交易对，如 BTC/USDT。base=买进来的币，quote=计价币。"""

    base = models.ForeignKey(
        Asset, on_delete=models.PROTECT, related_name="pairs_as_base"
    )
    quote = models.ForeignKey(
        Asset, on_delete=models.PROTECT, related_name="pairs_as_quote"
    )
    symbol = models.CharField("交易对符号", max_length=32, unique=True)
    tick_size = models.DecimalField(
        "最小报价单位", max_digits=18, decimal_places=8, default="0.01"
    )
    lot_size = models.DecimalField(
        "最小下单数量", max_digits=18, decimal_places=8, default="0.000001"
    )
    min_notional = models.DecimalField(
        "最小下单金额", max_digits=18, decimal_places=8, default="0"
    )
    is_active = models.BooleanField("是否启用", default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "交易对"
        verbose_name_plural = verbose_name
        constraints = [
            models.UniqueConstraint(fields=["base", "quote"], name="uniq_base_quote")
        ]

    def __str__(self):
        return self.symbol


class Order(models.Model):
    class Side(models.TextChoices):
        BUY = "BUY", "买入"
        SELL = "SELL", "卖出"

    class Type(models.TextChoices):
        LIMIT = "LIMIT", "限价单"
        MARKET = "MARKET", "市价单"

    class Status(models.TextChoices):
        NEW = "NEW", "待成交"
        PARTIALLY_FILLED = "PARTIALLY_FILLED", "部分成交"
        FILLED = "FILLED", "已成交"
        CANCELED = "CANCELED", "已撤销/剩余取消"

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="orders"
    )
    pair = models.ForeignKey(TradingPair, on_delete=models.PROTECT, related_name="orders")
    side = models.CharField("方向", max_length=8, choices=Side.choices)
    type = models.CharField("类型", max_length=8, choices=Type.choices)

    # limit_price：限价单价格；市价单为 NULL
    limit_price = models.DecimalField(
        "限价", max_digits=36, decimal_places=8, null=True, blank=True
    )
    # orig_qty：限价单=委托数量；市价买=委托买入预算(quote 数量)，市价卖=base 数量
    orig_qty = models.DecimalField("委托数量/预算", max_digits=36, decimal_places=8)
    filled_qty = models.DecimalField(
        "已成交(base)数量", max_digits=36, decimal_places=8, default=0
    )
    filled_quote = models.DecimalField(
        "已成交额(quote)", max_digits=36, decimal_places=8, default=0
    )
    # 剩余冻结：随成交释放。买单为 quote，卖单为 base
    remaining_frozen = models.DecimalField(
        "剩余冻结", max_digits=36, decimal_places=8, default=0
    )
    # 冻结发生在哪个账户（买=quote 账户，卖=base 账户）
    account = models.ForeignKey(
        Account, on_delete=models.PROTECT, related_name="frozen_orders"
    )

    status = models.CharField(
        "状态", max_length=16, choices=Status.choices, default=Status.NEW
    )
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "订单"
        verbose_name_plural = verbose_name
        indexes = [
            models.Index(fields=["pair", "status", "-id"]),
            models.Index(fields=["user", "-created_at"]),
        ]
        constraints = [
            CheckConstraint(check=Q(orig_qty__gt=0), name="chk_order_orig_qty"),
            CheckConstraint(check=Q(filled_qty__gte=0), name="chk_order_filled_qty"),
            CheckConstraint(
                check=Q(remaining_frozen__gte=0), name="chk_order_rem_frozen"
            ),
        ]

    @property
    def remaining_qty(self):
        """对限价单/市价卖：剩余 base 数量。"""
        return self.orig_qty - self.filled_qty

    @property
    def is_open(self):
        return self.status in (self.Status.NEW, self.Status.PARTIALLY_FILLED)

    def __str__(self):
        return f"#{self.id} {self.side} {self.type} {self.pair_id}"


class Trade(models.Model):
    """一笔成交（一条 taker 吃一个 maker）。"""

    pair = models.ForeignKey(TradingPair, on_delete=models.PROTECT, related_name="trades")
    taker_order = models.ForeignKey(
        Order, on_delete=models.PROTECT, related_name="taker_trades"
    )
    maker_order = models.ForeignKey(
        Order, on_delete=models.PROTECT, related_name="maker_trades"
    )
    price = models.DecimalField("成交价", max_digits=36, decimal_places=8)
    quantity = models.DecimalField("成交数量", max_digits=36, decimal_places=8)
    quote_amount = models.DecimalField("成交额", max_digits=36, decimal_places=8)
    taker_fee = models.DecimalField("taker手续费", max_digits=36, decimal_places=8)
    maker_fee = models.DecimalField("maker手续费", max_digits=36, decimal_places=8)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        verbose_name = "成交"
        verbose_name_plural = verbose_name
        indexes = [models.Index(fields=["pair", "-id"])]


class IdempotencyKey(models.Model):
    """
    下单幂等键：(user, key) 唯一。
    - 重复提交且参数完全一致：返回已存在的 order_id，不重复下单。
    - 同键但参数不同：409 冲突。
    """

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="idem_keys"
    )
    key = models.CharField("幂等键", max_length=128)
    order = models.ForeignKey(Order, on_delete=models.PROTECT, related_name="idem_keys")
    fingerprint = models.CharField("参数指纹", max_length=128)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "幂等键"
        verbose_name_plural = verbose_name
        constraints = [
            models.UniqueConstraint(fields=["user", "key"], name="uniq_user_idem_key")
        ]


class SystemConfig(models.Model):
    """全局单行配置：手续费率（小数，如 0.001 = 0.1%）与维护模式开关。"""

    singleton_id = models.PositiveSmallIntegerField(primary_key=True, default=1)
    taker_fee_rate = models.DecimalField(
        "taker费率", max_digits=10, decimal_places=8, default="0.001"
    )
    maker_fee_rate = models.DecimalField(
        "maker费率", max_digits=10, decimal_places=8, default="0.001"
    )
    maintenance_mode = models.BooleanField("维护模式", default=False)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "系统配置"
        verbose_name_plural = verbose_name

    @classmethod
    def load(cls) -> "SystemConfig":
        obj, _ = cls.objects.get_or_create(singleton_id=1)
        return obj


class AuditLog(models.Model):
    """关键操作审计：下单冲突、撤单、维护切换、费率变更等。"""

    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="audit_logs",
    )
    action = models.CharField("动作", max_length=48, db_index=True)
    target = models.CharField("对象", max_length=128, blank=True, default="")
    detail = models.TextField("详情", blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        verbose_name = "审计日志"
        verbose_name_plural = verbose_name
        indexes = [models.Index(fields=["action", "-created_at"])]
