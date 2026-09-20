"""
账户/账本服务层。所有余额变更必须经过本模块，视图与撮合引擎不得直接写余额。

设计要点：
1. 每个函数都假定调用方已开启 transaction.atomic()；函数内部对相关账户
   select_for_update 行锁，并按主键排序加锁，避免多账户交叉死锁。
2. 冻结 freeze/release 只在 available 与 frozen 之间划转，不写账本
   （资产权属未变化）。
3. 真实权属变化（成交、模拟发行）统一走 _post_legs：一个 tx_group 内
   按资产分别求和，每个资产的金额之和必须为 0（有借必有贷）。
   任何账户变化后 available/frozen 不得为负。
"""
import uuid
from decimal import Decimal

from django.contrib.auth.models import User

from .models import Account, Asset, LedgerEntry

# 保留系统用户名（数据迁移 0002 保证存在）；FEE/FUNDING 账户都挂该用户
SYSTEM_USERNAME = "SYSTEM"


class BalanceError(Exception):
    """余额不足或会导致负余额等非法状态。"""


class LedgerUnbalanced(Exception):
    """复式分录不守恒（同资产金额之和不为 0），属于严重程序错误。"""


def get_or_create_user_account(user, asset: Asset) -> Account:
    return Account.objects.get_or_create(
        user=user,
        asset=asset,
        defaults={"account_type": Account.AccountType.USER},
    )[0]


def get_system_user() -> User:
    # get_or_create 自愈：测试框架 flush、人工清库等情况下也能重新获得保留用户
    user, _ = User.objects.get_or_create(
        username=SYSTEM_USERNAME,
        defaults={"is_active": False, "is_staff": False, "is_superuser": False},
    )
    return user


def get_system_account(account_type: str, asset: Asset) -> Account:
    return Account.objects.get_or_create(
        user=get_system_user(), asset=asset, account_type=account_type
    )[0]


def lock_accounts(accounts) -> list[Account]:
    """按主键排序加行锁，保证多账户更新时加锁顺序全局一致。"""
    pks = sorted({a.pk for a in accounts if a is not None})
    return list(Account.objects.select_for_update().filter(pk__in=pks).order_by("pk"))


def freeze(account: Account, amount: Decimal) -> Account:
    """available -> frozen。调用方需已持有该账户行锁（或本函数自行加锁）。"""
    locked = lock_accounts([account])[0]
    if locked.available < amount:
        raise BalanceError(
            f"可用余额不足 account={locked.pk} available={locked.available} "
            f"need={amount}"
        )
    locked.available -= amount
    locked.frozen += amount
    locked.save(update_fields=["available", "frozen"])
    account.available = locked.available
    account.frozen = locked.frozen
    return locked


def unfreeze(account: Account, amount: Decimal) -> Account:
    """frozen -> available（撤单/市价单剩余/冻结零头退回）。"""
    locked = lock_accounts([account])[0]
    if locked.frozen < amount:
        raise BalanceError(
            f"冻结余额不足 account={locked.pk} frozen={locked.frozen} release={amount}"
        )
    locked.frozen -= amount
    locked.available += amount
    locked.save(update_fields=["available", "frozen"])
    account.available = locked.available
    account.frozen = locked.frozen
    return locked


def _post_legs(legs, ref_type: str, ref_id: str, tx_group=None):
    """
    提交一组复式分录并原子更新账户余额。

    leg = (account, asset, amount(带符号), memo, from_frozen)
      amount > 0：资产增加，记入 available；
      amount < 0：资产减少，from_frozen=True 时扣 frozen，否则扣 available。
    按 asset 分组，每组金额之和必须为 0。
    """
    tx_group = tx_group or uuid.uuid4()
    locked = {a.pk: a for a in lock_accounts([leg[0] for leg in legs])}

    sums: dict = {}
    for account, asset, amount, _memo, _ff in legs:
        sums[asset.pk] = sums.get(asset.pk, Decimal("0")) + amount
    for asset_pk, total in sums.items():
        if total != 0:
            raise LedgerUnbalanced(f"资产 {asset_pk} 分录不守恒: {total}")

    entries = []
    for seq, (account, asset, amount, memo, from_frozen) in enumerate(legs):
        acc = locked[account.pk]
        if amount > 0:
            acc.available += amount
        elif from_frozen:
            acc.frozen += amount  # amount 为负
        else:
            acc.available += amount
        # FUNDING 发行账户允许为负（=-累计发行量）
        if acc.account_type != Account.AccountType.FUNDING:
            if acc.available < 0 or acc.frozen < 0:
                raise BalanceError(
                    f"记账导致负余额 account={acc.pk} available={acc.available} "
                    f"frozen={acc.frozen}"
                )
        acc.save(update_fields=["available", "frozen"])
        entries.append(
            LedgerEntry(
                tx_group=tx_group,
                seq=seq,
                account_id=acc.pk,
                asset_id=asset.pk,
                amount=amount,
                memo=memo,
                ref_type=ref_type,
                ref_id=str(ref_id),
            )
        )
    LedgerEntry.objects.bulk_create(entries)
    return tx_group, entries


def issue_simulated_asset(user, asset: Asset, amount: Decimal) -> None:
    """
    模拟充值/发行：用户 +amount（available），FUNDING 发行账户 -amount。
    全系统按资产加总恒为 0。
    """
    user_acc = get_or_create_user_account(user, asset)
    funding = get_system_account(Account.AccountType.FUNDING, asset)
    _post_legs(
        [
            (user_acc, asset, amount, "模拟资产发行", False),
            (funding, asset, -amount, "模拟资产发行(发行方)", False),
        ],
        ref_type="DEPOSIT",
        ref_id=f"sim:{user.pk}:{asset.symbol}:{amount}",
    )


def settle_fill(
    *,
    taker,
    maker,
    accounts_by_key: dict,
    base: Asset,
    quote: Asset,
    base_qty: Decimal,
    quote_amount: Decimal,
    taker_fee: Decimal,
    maker_fee: Decimal,
    fee_base_acc: Account,
    fee_quote_acc: Account,
    ref_id: str,
):
    """
    一笔 taker 吃 maker 成交的原子结算。

    费用从“收到的资产”中扣除：
      taker 买: taker 收到 base，扣 taker_fee（base）；
                maker 收到 quote，扣 maker_fee（quote）。
      taker 卖: taker 收到 quote，扣 taker_fee（quote）；
                maker 收到 base，扣 maker_fee（base）。
    买入方的 quote 支出 / 卖出方的 base 支出均来自下单冻结（from_frozen=True）。

    accounts_by_key: {(user_id, asset_id): Account}
    返回 (tx_group, entries)，由调用方包在成交事务内。
    """
    tb = accounts_by_key[(taker.user_id, base.pk)]
    tq = accounts_by_key[(taker.user_id, quote.pk)]
    mb = accounts_by_key[(maker.user_id, base.pk)]
    mq = accounts_by_key[(maker.user_id, quote.pk)]

    legs = []
    if taker.side == "BUY":
        # base 流：maker 卖出（冻结出账） -> taker 收（扣费） -> 手续费账户
        legs.append((mb, base, -base_qty, "maker卖出base(冻结出账)", True))
        if base_qty - taker_fee > 0:
            legs.append(
                (tb, base, base_qty - taker_fee, "taker买入收到base(扣费)", False)
            )
        if taker_fee > 0:
            legs.append((fee_base_acc, base, taker_fee, "taker买入手续费", False))

        # quote 流：taker 买入付出（冻结出账） -> maker 收（扣费） -> 手续费账户
        legs.append((tq, quote, -quote_amount, "taker买入付出quote(冻结出账)", True))
        if quote_amount - maker_fee > 0:
            legs.append(
                (mq, quote, quote_amount - maker_fee, "maker卖出收到quote(扣费)", False)
            )
        if maker_fee > 0:
            legs.append((fee_quote_acc, quote, maker_fee, "maker卖出手续费", False))
    else:
        # taker 卖：base 由 taker 冻结出账，maker 买 base（扣费）
        legs.append((tb, base, -base_qty, "taker卖出base(冻结出账)", True))
        if base_qty - maker_fee > 0:
            legs.append(
                (mb, base, base_qty - maker_fee, "maker买入收到base(扣费)", False)
            )
        if maker_fee > 0:
            legs.append((fee_base_acc, base, maker_fee, "maker买入手续费", False))

        # quote：maker 冻结出账，taker 收 quote（扣费）
        legs.append((mq, quote, -quote_amount, "maker买入付出quote(冻结出账)", True))
        if quote_amount - taker_fee > 0:
            legs.append(
                (tq, quote, quote_amount - taker_fee, "taker卖出收到quote(扣费)", False)
            )
        if taker_fee > 0:
            legs.append((fee_quote_acc, quote, taker_fee, "taker卖出手续费", False))

    return _post_legs(legs, ref_type="TRADE", ref_id=ref_id)
