"""Matching engine: order placement, matching, cancellation, maintenance.

Concurrency model
-----------------
All operations on one market serialise on the ``markets`` row via
``SELECT ... FOR UPDATE`` (MySQL InnoDB row lock).  Different markets use
different rows and run in parallel.  Inside the critical section:

1. balances and resting orders are read with ``select_for_update``;
2. balances, orders, trades/fills and ledger events are written in one
   atomic transaction;
3. the in-memory book is mutated only after the commit.

Therefore a fill and a cancel racing for the same resting order cannot both
succeed: whoever waits sees the committed outcome and either fills against a
locked order or finds it no longer working.

Crash safety
------------
* A crash before commit rolls everything back (freeze included -- no order
  exists without its freeze and no fill exists without its settlement).
* A crash after commit is durable: fills carry a unique
  ``(trade, order)`` constraint and order progress updates share the
  transaction, so the same trade can never settle twice.
* On restart the in-memory book is rebuilt from persisted working orders in
  price-time order, so no resting order is lost or reordered.
"""
import hashlib
import json
from dataclasses import dataclass, field
from decimal import Decimal

from django.db import IntegrityError, transaction

from apps.accounts.models import Balance
from apps.accounts.services import (
    freeze,
    get_or_create_system_user,
    lock_balances,
    release,
    settle_fill,
)
from apps.audit.services import record
from apps.common.decimals import (
    D8,
    ZERO,
    fee_from_bps,
    limit_buy_freeze,
    multiply_price_qty,
    quantize_floor,
    quantize_half_up,
)
from apps.ledger.models import LedgerEvent
from apps.ledger.services import (
    freeze_entries,
    post_event,
    release_entries,
    trade_entries,
)
from apps.markets.models import Market
from apps.trading.book_registry import registry
from apps.trading.models import Fill, IdempotencyKey, Order, Trade
from apps.trading.orderbook import WORKING

# Ordering of balance rows is global by (user_id, asset_id) inside
# lock_balances; the market lock already serialises same-pair traffic.


class OrderRejected(Exception):
    """Order rejected during validation/freeze (400 to the client)."""


class Conflict(Exception):
    """Idempotency key reused with different parameters (409)."""


class OrderNotCancelable(Exception):
    """Order already filled/canceled (409)."""


class MaintenanceConflict(Exception):
    """Raised when maintenance racing with orders hits a lock conflict."""


@dataclass
class MatchReport:
    order: Order = None
    trades: list = field(default_factory=list)
    replayed: bool = False  # idempotent replay, not newly matched
    # Book mutations to apply AFTER commit: (maker id, price, side,
    # remaining qty) for every resting order hit (removed when fully filled,
    # quantity refreshed otherwise), plus the taker if it rests.  Kept out
    # of the pre-commit phase so a rollback never leaves the cache diverging
    # from persisted state.
    filled_makers: list = field(default_factory=list)
    taker_rests: bool = False


def _request_hash(payload: dict) -> str:
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode()).hexdigest()


def _lock_market(market_id: int) -> Market:
    return Market.objects.select_for_update().get(id=market_id)


def _fee_user():
    from django.conf import settings

    user, _ = get_or_create_system_user(settings.FEE_ACCOUNT_USERNAME)
    return user


def _validate_order_input(market, type_, side, price, quantity, quote_amount):
    if not market.is_active:
        raise OrderRejected("market is disabled")
    if market.in_maintenance:
        raise OrderRejected("market is in maintenance mode")

    if type_ == Order.OrderType.LIMIT:
        if price is None or price <= 0:
            raise OrderRejected("limit order requires a positive price")
        if quantity is None or quantity <= 0:
            raise OrderRejected("limit order requires a positive quantity")
        if price.as_tuple().exponent < -8:
            raise OrderRejected("price may have at most 8 decimal places")
        if quantity.as_tuple().exponent < -8:
            raise OrderRejected("quantity may have at most 8 decimal places")
        if quantity < market.min_quantity:
            raise OrderRejected(
                f"quantity below minimum {market.min_quantity}"
            )
        if quantize_half_up(price * quantity) < market.min_notional:
            raise OrderRejected(
                f"notional below minimum {market.min_notional}"
            )
    else:  # MARKET
        if side == Order.Side.SELL:
            if quantity is None or quantity <= 0:
                raise OrderRejected("market sell requires a positive quantity")
            if quantity.as_tuple().exponent < -8:
                raise OrderRejected("quantity may have at most 8 decimal places")
            if quantity < market.min_quantity:
                raise OrderRejected(
                    f"quantity below minimum {market.min_quantity}"
                )
        else:
            if quote_amount is None or quote_amount <= 0:
                raise OrderRejected(
                    "market buy requires positive quote_amount (funds to spend)"
                )
            if quote_amount.as_tuple().exponent < -8:
                raise OrderRejected(
                    "quote_amount may have at most 8 decimal places"
                )
            if quote_amount < market.min_notional:
                raise OrderRejected(
                    f"quote_amount below minimum notional {market.min_notional}"
                )


@transaction.atomic
def submit_order(
    *,
    user,
    market: Market,
    type: str,
    side: str,
    price=None,
    quantity=None,
    quote_amount=None,
    idempotency_key=None,
    request_payload=None,
    ip_address=None,
) -> MatchReport:
    # ---- idempotency fast path (may be a concurrent first inserter too) ---
    if idempotency_key:
        digest = _request_hash(request_payload)
        existing = IdempotencyKey.objects.filter(
            user=user, key=idempotency_key
        ).select_related("order").first()
        if existing is not None:
            if existing.request_hash != digest:
                record(
                    "IDEMPOTENCY_CONFLICT",
                    actor=user,
                    target=idempotency_key,
                    detail={"stored_hash": existing.request_hash, "got": digest},
                    ip_address=ip_address,
                )
                raise Conflict("idempotency key reused with different parameters")
            return MatchReport(order=existing.order, replayed=True)
    else:
        digest = None

    # ---- serialise this market -------------------------------------------
    market = _lock_market(market.id)
    # The in-memory book is a cache shared across threads.  Another thread
    # may have committed a cancel/fill between this thread's cache read and
    # acquiring the market lock; under the lock the database is the truth,
    # so rebuild the book before matching.  This is bounded by the number
    # of resting orders on one pair and only happens inside the critical
    # section, so it stays consistent (price-time order comes from the
    # (price, sequence) index).
    book = registry.rebuild(market.id)

    # Re-check the idempotency key *after* taking the market lock.  Two
    # concurrent requests for the same user+key serialise on this same row;
    # the loser now sees the winner's committed key here and replays before
    # doing any freeze, so the post-insert IntegrityError path is only a
    # belt-and-braces fallback for different markets sharing a key.
    if idempotency_key:
        existing = (
            IdempotencyKey.objects.select_for_update()
            .select_related("order")
            .filter(user=user, key=idempotency_key)
            .first()
        )
        if existing is not None:
            if existing.request_hash != digest:
                record(
                    "IDEMPOTENCY_CONFLICT",
                    actor=user,
                    target=idempotency_key,
                    detail={
                        "stored_hash": existing.request_hash, "got": digest
                    },
                    ip_address=ip_address,
                )
                raise Conflict(
                    "idempotency key reused with different parameters"
                )
            return MatchReport(order=existing.order, replayed=True)

    _validate_order_input(
        market, type, side,
        quantize_half_up(price) if price is not None else None,
        quantize_half_up(quantity) if quantity is not None else None,
        quantize_half_up(quote_amount) if quote_amount is not None else None,
    )
    base, quote = market.base_asset, market.quote_asset

    # ---- freeze -----------------------------------------------------------
    if side == Order.Side.BUY:
        if type == Order.OrderType.LIMIT:
            # Single HALF_UP rounding of the gross quote cost.  Per-fill
            # costs are floored, so this freeze always covers all fills;
            # buyer fees are taken from base received, never from quote.
            freeze_amount = limit_buy_freeze(price, quantity)
        else:
            freeze_amount = quantize_half_up(quote_amount)
        frozen_asset = quote
    else:
        freeze_amount = quantize_half_up(quantity)
        frozen_asset = base

    # ---- create order + freeze inside a savepoint so a duplicate-key loser
    # can roll back only its own work, not the caller's transaction ---------
    sid = transaction.savepoint()
    balance = _locked_balance(user, frozen_asset)
    try:
        freeze(balance, freeze_amount)
    except Exception as exc:
        transaction.savepoint_rollback(sid)
        raise OrderRejected(str(exc)) from exc
    post_event(
        LedgerEvent.EventType.FREEZE,
        freeze_entries(user, frozen_asset, freeze_amount, balance),
        note=f"order freeze {side} {market.symbol}",
    )

    order = Order.objects.create(
        user=user,
        market=market,
        type=type,
        side=side,
        price=price if type == Order.OrderType.LIMIT else None,
        quantity=quantity if quantity is not None else None,
        quote_amount=(
            quote_amount
            if type == Order.OrderType.MARKET and side == Order.Side.BUY
            else None
        ),
        frozen_remaining=freeze_amount,
        status=Order.Status.NEW,
    )
    order.sequence = order.id
    order.save(update_fields=["sequence"])

    if idempotency_key:
        try:
            IdempotencyKey.objects.create(
                user=user, key=idempotency_key, order=order,
                request_hash=digest,
            )
        except IntegrityError:
            # A concurrent request with the same key won.  Discard our
            # freeze/order and replay the winner's order.  Use
            # select_for_update as a *current read*: under MySQL
            # REPEATABLE READ an ordinary SELECT would keep our stale
            # snapshot and not see the winner's just-committed row.
            transaction.savepoint_rollback(sid)
            dup = (
                IdempotencyKey.objects.select_for_update()
                .select_related("order")
                .get(user=user, key=idempotency_key)
            )
            if dup.request_hash != digest:
                raise Conflict(
                    "idempotency key reused with different parameters"
                )
            return MatchReport(order=dup.order, replayed=True)
    transaction.savepoint_commit(sid)

    # ---- match ------------------------------------------------------------
    # ``book`` was rebuilt from committed state just after taking the market
    # lock above, so it reflects every cancel/fill that raced with us.
    report = MatchReport(order=order)

    if type == Order.OrderType.LIMIT:
        _match_limit_taker(order, market, base, quote, book, report)
    else:
        _match_market_taker(order, market, base, quote, book, report)

    return report


def _locked_balance(user, asset) -> Balance:
    from apps.accounts.services import get_or_create_balance

    get_or_create_balance(user, asset)
    return Balance.objects.select_for_update().get(user=user, asset=asset)


def _maker_limit_price_ok(side, taker_price, maker_price) -> bool:
    if side == Order.Side.BUY:
        return maker_price <= taker_price
    return maker_price >= taker_price


def _execute_fill(
    *, taker, maker, market, base, quote, price, qty, taker_side, report
):
    """Settle one taker-vs-maker fill, all rows already locked by caller."""
    cost = multiply_price_qty(price, qty)

    taker_is_buy = taker_side == Order.Side.BUY
    # Fees are charged in the asset each side receives, HALF_UP at 8 dp.
    taker_fee = fee_from_bps(
        qty if taker_is_buy else cost, market.taker_fee_bps
    )
    maker_fee = fee_from_bps(
        cost if taker_is_buy else qty, market.maker_fee_bps
    )

    fee_user = _fee_user()
    pairs = [
        (taker.user, base), (taker.user, quote),
        (maker.user, base), (maker.user, quote),
        (fee_user, base), (fee_user, quote),
    ]
    balances = lock_balances(pairs)
    taker_bal = {
        k: v for k, v in balances.items() if k[0] == taker.user_id
    }
    maker_bal = {
        k: v for k, v in balances.items() if k[0] == maker.user_id
    }
    fee_bal = {
        k: v for k, v in balances.items() if k[0] == fee_user.id
    }

    settle_fill(
        taker_balances=taker_bal,
        maker_balances=maker_bal,
        fee_balances=fee_bal,
        taker=taker.user,
        maker=maker.user,
        fee_user=fee_user,
        side=taker_side,
        base_asset=base,
        quote_asset=quote,
        cost=cost,
        quantity=qty,
        taker_fee=taker_fee,
        maker_fee=maker_fee,
    )

    trade = Trade.objects.create(
        market=market,
        taker_order=taker,
        maker_order=maker,
        price=price,
        quantity=qty,
        taker_fee=taker_fee,
        maker_fee=maker_fee,
    )
    Fill.objects.bulk_create(
        [
            Fill(
                trade=trade, order=taker, role=Fill.Role.TAKER,
                side=taker_side, price=price, quantity=qty, fee=taker_fee,
            ),
            Fill(
                trade=trade, order=maker,
                role=Fill.Role.MAKER,
                side=(Order.Side.SELL if taker_is_buy else Order.Side.BUY),
                price=price, quantity=qty, fee=maker_fee,
            ),
        ]
    )

    # --- order progress ----------------------------------------------------
    taker.filled_quantity += qty
    maker.filled_quantity += qty

    # Freeze accounting: the frozen-remaining column holds the part of the
    # order's original freeze not yet consumed by fills (buy: quote; sell:
    # base).  Fee reserves included in a limit buy's freeze are reconciled
    # when the order rests / cancels.
    if taker_side == Order.Side.BUY:
        taker.frozen_remaining -= cost
        maker.frozen_remaining -= qty  # seller's base freeze
    else:
        taker.frozen_remaining -= qty  # seller's base freeze
        maker.frozen_remaining -= cost  # buyer's quote freeze

    # Status from remaining quantity (limit orders and market sells carry a
    # target quantity; market buys are finalized by their caller).
    for o in (taker, maker):
        if o.type == Order.OrderType.MARKET and o.side == Order.Side.BUY:
            continue
        target = o.quantity
        if o.filled_quantity >= target:
            o.status = Order.Status.FILLED
        elif o.filled_quantity > ZERO:
            o.status = Order.Status.PARTIALLY_FILLED

    # A terminal buy can hold residual quote freeze (the fee reserve added
    # at placement, plus any rounding dust); give it back.  A resting maker
    # keeps exactly price*remaining frozen (reconciled when it first rests).
    residual_orders = []
    for o in (taker, maker):
        if (
            not o.is_working
            and o.side == Order.Side.BUY
            and o.frozen_remaining > ZERO
        ):
            residual_orders.append(o)
    for o in residual_orders:
        _release_frozen(
            o, quote, o.frozen_remaining,
            ledger_type=LedgerEvent.EventType.RELEASE,
        )

    taker.save()
    maker.save()

    entries = trade_entries(
        taker=taker.user,
        maker=maker.user,
        fee_user=fee_user,
        side=taker_side,
        base_asset=base,
        quote_asset=quote,
        cost=cost,
        quantity=qty,
        taker_fee=taker_fee,
        maker_fee=maker_fee,
        taker_balances=taker_bal,
        maker_balances=maker_bal,
        fee_balances=fee_bal,
    )
    post_event(
        LedgerEvent.EventType.TRADE, entries, order=taker, trade=trade,
        note=f"fill {market.symbol} {qty}@{price}",
    )
    report.trades.append(trade)
    report.filled_makers.append(
        (maker.id, maker.price, maker.side, maker.remaining_quantity)
    )
    return trade, cost, taker_fee, maker_fee


def _lock_resting_order(order_id: int) -> Order:
    return Order.objects.select_for_update().get(id=order_id)


def _match_limit_taker(order, market, base, quote, book, report):
    resting = book.bids if order.side == Order.Side.SELL else book.asks

    for level in list(resting.best_iter()):
        if not order.is_working:
            break
        if order.remaining_quantity <= ZERO:
            break
        if not _maker_limit_price_ok(order.side, order.price, level.price):
            break
        maker = _lock_resting_order(level.order_id)
        if not maker.is_working or maker.price != level.price:
            # Stale cache entry (filled/canceled in another transaction this
            # process didn't see, or a partial structural miss): drop it.
            resting.remove(level.order_id, level.price)
            continue
        qty = min(order.remaining_quantity, maker.remaining_quantity)
        price = maker.price  # price-time matching at the resting price
        _execute_fill(
            taker=order, maker=maker, market=market, base=base, quote=quote,
            price=price, qty=qty, taker_side=order.side, report=report,
        )

    if order.is_working and order.remaining_quantity > ZERO:
        # Resting remainder.  Reconcile the quote freeze: a buy's initial
        # freeze carried a worst-case *taker fee reserve* (quote), but fees
        # are actually charged in the asset received (base for buyers), so
        # only the gross cost ``price * remaining_qty`` must stay frozen.
        # For sells the freeze equals remaining base quantity already.
        if order.side == Order.Side.BUY:
            needed = multiply_price_qty(order.price, order.remaining_quantity)
            surplus = order.frozen_remaining - needed
            if surplus > ZERO:
                _release_frozen(order, quote, surplus,
                                ledger_type=LedgerEvent.EventType.RELEASE)
        report.taker_rests = True
        order.status = (
            Order.Status.PARTIALLY_FILLED if order.filled_quantity > ZERO
            else Order.Status.NEW
        )
        order.save()


def _match_market_taker(order, market, base, quote, book, report):
    """Match a market order; it never rests.

    * SELL: match until the requested base quantity is gone or the book is
      empty; leftover is canceled and its base freeze released.
    * BUY: spend up to ``quote_amount`` quote (fees are taken from base
      received); consume asks cheapest-first; leftover quote is released.
    """
    if order.side == Order.Side.SELL:
        _match_market_sell(order, market, base, quote, book, report)
    else:
        _match_market_buy(order, market, base, quote, book, report)

    filled = order.filled_quantity > ZERO
    if order.side == Order.Side.BUY:
        fully_consumed = order.frozen_remaining <= ZERO
    else:
        fully_consumed = order.filled_quantity >= order.quantity

    if filled and fully_consumed:
        order.status = Order.Status.FILLED
    else:
        # No fills at all, or a remainder the thin book cannot absorb:
        # market-order remainder is canceled immediately per spec.
        order.status = Order.Status.CANCELED

    if order.frozen_remaining > ZERO:
        asset = quote if order.side == Order.Side.BUY else base
        _release_frozen(
            order, asset, order.frozen_remaining,
            ledger_type=LedgerEvent.EventType.RELEASE,
        )
    order.save()


def _match_market_sell(order, market, base, quote, book, report):
    for level in list(book.bids.best_iter()):
        if order.filled_quantity >= order.quantity:
            break
        maker = _lock_resting_order(level.order_id)
        if not maker.is_working:
            book.bids.remove(level.order_id, level.price)
            continue
        qty = min(order.quantity - order.filled_quantity,
                  maker.remaining_quantity)
        _execute_fill(
            taker=order, maker=maker, market=market, base=base, quote=quote,
            price=maker.price, qty=qty, taker_side=Order.Side.SELL,
            report=report,
        )


def _match_market_buy(order, market, base, quote, book, report):
    budget = Decimal(order.quote_amount)
    spent = ZERO
    for level in list(book.asks.best_iter()):
        remaining_budget = budget - spent
        if remaining_budget < D8:
            break
        maker = _lock_resting_order(level.order_id)
        if not maker.is_working:
            book.asks.remove(level.order_id, level.price)
            continue
        price = maker.price
        # FLOOR: never spend more quote than the frozen budget through
        # rounding.  Fee for the buyer is taken from base received, so it
        # does not consume the quote budget.
        max_affordable = quantize_floor(remaining_budget / price)
        if max_affordable <= ZERO:
            break
        qty = min(maker.remaining_quantity, max_affordable)
        cost = multiply_price_qty(price, qty)  # floored -> cost <= budget
        _execute_fill(
            taker=order, maker=maker, market=market, base=base, quote=quote,
            price=price, qty=qty, taker_side=Order.Side.BUY, report=report,
        )
        spent += cost

    # frozen_remaining already tracks budget - sum(cost) via each fill.
    order.frozen_remaining = budget - spent


def _release_frozen(order, asset, amount, *, ledger_type):
    from apps.accounts.services import get_or_create_balance

    amount = Decimal(amount)
    get_or_create_balance(order.user, asset)
    balance = Balance.objects.select_for_update().get(
        user=order.user, asset=asset
    )
    release(balance, amount)
    order.frozen_remaining -= amount
    post_event(
        ledger_type,
        release_entries(order.user, asset, amount, balance),
        order=order,
    )
    return balance


@transaction.atomic
def cancel_order(*, user, order_id, ip_address=None) -> Order:
    """Cancel a working order; safe to repeat and safe against fills.

    The market row is locked first (same critical section as matching),
    then the order is locked.  Outcomes:

    * order belongs to someone else -> 404 (users only see their own);
    * order already terminal -> 409; the client learns the real status;
    * otherwise the remaining freeze is released in a balanced ledger
      event and the order becomes CANCELED.
    """
    order = (
        Order.objects.select_related("market")
        .filter(id=order_id, user=user)
        .first()
    )
    if order is None:
        raise Order.DoesNotExist()

    market = _lock_market(order.market_id)  # serialise with matching
    order = (
        Order.objects.select_for_update()
        .select_related("market")
        .get(id=order_id)
    )
    if not order.is_working:
        raise OrderNotCancelable(
            f"order is {order.status}; only working orders can be canceled"
        )

    base, quote = market.base_asset, market.quote_asset
    asset = quote if order.side == Order.Side.BUY else base
    if order.frozen_remaining > ZERO:
        _release_frozen(order, asset, order.frozen_remaining,
                        ledger_type=LedgerEvent.EventType.RELEASE)

    order.status = Order.Status.CANCELED
    order.save()
    return order

@transaction.atomic
def set_maintenance(*, admin_user, market_id, enabled: bool, ip_address=None):
    """Toggle maintenance mode for one market.

    On: reject new orders (checked in submit), cancel ALL resting orders
    and release their freezes.  Idempotent: repeating "on" just re-affirms
    the flag (there is nothing left to cancel) and repeating "off" is a
    no-op.  Every cancellation is an independent balanced ledger event
    inside this one transaction -- either the whole maintenance switch is
    applied or none of it.
    """
    market = _lock_market(market_id)
    if enabled:
        market.in_maintenance = True
        market.save(update_fields=["in_maintenance", "updated_at"])
        canceled_ids = []
        resting_ids = list(
            Order.objects.filter(market=market, status__in=WORKING)
            .values_list("id", flat=True)
        )
        for oid in resting_ids:
            order = (
                Order.objects.select_for_update().get(id=oid)
            )
            if not order.is_working:
                continue
            asset = (
                market.quote_asset if order.side == Order.Side.BUY
                else market.base_asset
            )
            if order.frozen_remaining > ZERO:
                _release_frozen(
                    order, asset, order.frozen_remaining,
                    ledger_type=LedgerEvent.EventType.MAINTENANCE,
                )
            order.status = Order.Status.CANCELED
            order.save()
            canceled_ids.append(oid)
        # The book cache is invalidated by the view AFTER commit.
        record(
            "MAINTENANCE_ON",
            actor=admin_user,
            target=market.symbol,
            detail={"canceled_order_ids": canceled_ids},
            ip_address=ip_address,
        )
        return market, canceled_ids

    market.in_maintenance = False
    market.save(update_fields=["in_maintenance", "updated_at"])
    record(
        "MAINTENANCE_OFF",
        actor=admin_user, target=market.symbol, detail={},
        ip_address=ip_address,
    )
    return market, []


# Called by views AFTER a successful commit so the cache never diverges from
# committed state for the structural changes (rest / fill / cancel).
def apply_book_changes(market_id, report):
    """Refresh the cached book after a committed order.

    The cache is only used by the public order-book endpoint -- matching
    always rebuilds under the market lock -- so a simple invalidation is
    enough and cannot diverge from committed state.
    """
    registry.invalidate(market_id)
    return registry.get(market_id)


def sync_book_after_commit(market_id, order=None, canceled=False):
    registry.invalidate(market_id)
    return registry.get(market_id)
