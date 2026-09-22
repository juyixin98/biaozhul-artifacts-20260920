"""Process-local registry of in-memory order books.

Deployment runs gunicorn with **1 worker + N threads** (see
``docker-compose.yml``), so this registry is shared by every request.  The
books are caches nonetheless: the engine rebuilds any missing book from
persisted state and mutates the cache only after successful commits, which
keeps it correct across worker restarts and (at the cost of a rebuild) even
across multiple processes.
"""
import threading

from apps.trading.orderbook import OrderBook


class BookRegistry:
    def __init__(self):
        self._books = {}
        self._lock = threading.RLock()

    @property
    def lock(self):
        return self._lock

    def get(self, market_id) -> OrderBook:
        with self._lock:
            book = self._books.get(market_id)
            if book is None:
                book = OrderBook.rebuild(market_id)
                self._books[market_id] = book
            return book

    def invalidate(self, market_id):
        with self._lock:
            self._books.pop(market_id, None)

    def rebuild(self, market_id) -> OrderBook:
        with self._lock:
            book = OrderBook.rebuild(market_id)
            self._books[market_id] = book
            return book

    def snapshot(self, market_id, depth=50):
        """Depth snapshot under the registry lock (safe vs concurrent match)."""
        with self._lock:
            book = self.get(market_id)
            return book.snapshot(depth=depth)

    def warm_all(self):
        """Load every active market's book at process startup."""
        from apps.markets.models import Market

        with self._lock:
            for market in Market.objects.filter(is_active=True):
                self._books[market.id] = OrderBook.rebuild(market.id)
        return len(self._books)


registry = BookRegistry()
