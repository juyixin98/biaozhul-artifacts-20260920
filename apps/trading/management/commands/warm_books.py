"""Warm the in-memory order books from persisted state (startup hook)."""
from django.core.management.base import BaseCommand

from apps.trading.book_registry import registry


class Command(BaseCommand):
    help = "Rebuild every active market's in-memory order book from the database."

    def handle(self, *args, **opts):
        count = registry.warm_all()
        self.stdout.write(self.style.SUCCESS(
            f"recovered {count} order book(s) in price-time order"
        ))
