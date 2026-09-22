"""In-memory price-time-priority order book.

The book is a *cache*.  Every order submission holds a row lock on its
:class:`~apps.markets.models.Market`, and inside that lock the book is
synced from the database (``rebuild`` on first touch after process start or
after any structural miss), so concurrent processes / post-crash restarts
cannot trade against stale state.

Priority rules (the stated business rule):

1. **Price priority** -- the most aggressive resting price wins (highest bid
   for a sell taker, lowest ask for a buy taker).
2. **Time priority at the same price** -- older order first, where age is
   ``(created_at, sequence=order id)``.

Resting orders are represented by lightweight :class:`BookLevel` entries
holding ``order_id``; the engine re-reads each order under the DB lock
(``select_for_update``) before matching against it, so a cancel that
committed is always observed and fill/cancel can only end one way.
"""
from collections import OrderedDict
from decimal import Decimal

from django.db.models import QuerySet

from apps.trading.models import Order

WORKING = (Order.Status.NEW, Order.Status.PARTIALLY_FILLED)


class BookLevel:
    __slots__ = ("order_id", "remaining", "price", "sequence", "created_at")

    def __init__(self, order_id, remaining, price, sequence, created_at):
        self.order_id = order_id
        self.remaining = Decimal(remaining)
        self.price = Decimal(price)
        self.sequence = sequence
        self.created_at = created_at


class SideBook:
    """One side (bids or asks) of a pair, kept in price-time order."""

    def __init__(self, descending: bool):
        self.descending = descending
        # price -> OrderedDict(order_id -> BookLevel), price keys sorted.
        self.levels: "OrderedDict[Decimal, OrderedDict[int, BookLevel]]" = (
            OrderedDict()
        )

    def _sorted_prices(self):
        return sorted(self.levels.keys(), reverse=self.descending)

    def add(self, level: BookLevel):
        bucket = self.levels.get(level.price)
        if bucket is None:
            bucket = OrderedDict()
            self.levels[level.price] = bucket
        bucket[level.order_id] = level
        self.levels = OrderedDict(
            (p, self.levels[p]) for p in self._sorted_prices()
        )

    def remove(self, order_id, price):
        bucket = self.levels.get(price)
        if bucket and order_id in bucket:
            del bucket[order_id]
            if not bucket:
                del self.levels[price]

    def best_iter(self):
        """Yield resting levels best-price-first, FIFO within price."""
        for price in self._sorted_prices():
            bucket = self.levels[price]
            for level in list(bucket.values()):
                yield level

    def best_price(self):
        prices = self._sorted_prices()
        return prices[0] if prices else None

    def depth(self):
        rows = []
        for price in self._sorted_prices():
            qty = sum((l.remaining for l in self.levels[price].values()), Decimal(0))
            rows.append((price, qty))
        return rows

    def __len__(self):
        return sum(len(b) for b in self.levels.values())


class OrderBook:
    def __init__(self, market_id: int):
        self.market_id = market_id
        self.bids = SideBook(descending=True)
        self.asks = SideBook(descending=False)

    # -- recovery -----------------------------------------------------------

    @classmethod
    def rebuild(cls, market_id: int) -> "OrderBook":
        """Rebuild the book purely from persisted, working orders.

        Ordering comes from the DB index
        ``(market, side, status, price, sequence)``; sequence == order id,
        so within a price level earlier orders come first -- the original
        price-time ordering is preserved exactly across a restart.
        """
        book = cls(market_id)
        qs: QuerySet = (
            Order.objects.filter(
                market_id=market_id,
                status__in=WORKING,
            )
            # Market BUY orders are quote-funded (quantity is NULL) and can
            # never rest; exclude them so rebuild never sees one mid-flight.
            .exclude(quantity__isnull=True)
            .order_by("price", "sequence")
            .values(
                "id", "side", "price", "quantity", "filled_quantity",
                "sequence", "created_at",
            )
        )
        for row in qs:
            remaining = row["quantity"] - row["filled_quantity"]
            if remaining <= 0:
                continue
            level = BookLevel(
                order_id=row["id"],
                remaining=remaining,
                price=row["price"],
                sequence=row["sequence"] or row["id"],
                created_at=row["created_at"],
            )
            (book.bids if row["side"] == Order.Side.BUY else book.asks).add(level)
        return book

    # -- live mutation (caller already owns the market lock) ---------------

    def add_order(self, order: Order):
        remaining = order.remaining_quantity
        level = BookLevel(
            order_id=order.id,
            remaining=remaining,
            price=order.price,
            sequence=order.sequence or order.id,
            created_at=order.created_at,
        )
        (self.bids if order.side == Order.Side.BUY else self.asks).add(level)

    def remove_order(self, order):
        side = self.bids if order.side == Order.Side.BUY else self.asks
        price = order.price
        if price is None:
            return
        side.remove(order.id, price)

    def snapshot(self, depth=50):
        def f8(value):
            # Fixed 8-dp string: Decimal.__str__ would strip trailing zeros.
            return f"{value:.8f}"

        return {
            "bids": [
                [f8(p), f8(q)] for p, q in self.bids.depth()[:depth]
            ],
            "asks": [
                [f8(p), f8(q)] for p, q in self.asks.depth()[:depth]
            ],
        }
