"""Base test case: sqlite for speed; MySQL-only tests skip themselves."""
from django.db import connection
from django.test import TransactionTestCase

from apps.trading.book_registry import registry


class EngineTestCase(TransactionTestCase):
    """Transaction-level isolation; registry is reset between tests."""

    reset_sequences = True

    def setUp(self):
        registry._books.clear()

    def tearDown(self):
        registry._books.clear()

    @property
    def is_mysql(self):
        return connection.vendor == "mysql"
