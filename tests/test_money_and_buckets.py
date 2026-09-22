"""Fixed-point money helpers and stable bucketing properties."""
from decimal import Decimal

from django.test import SimpleTestCase

from common.utils import (
    MAX_MONEY,
    stable_bucket,
    to_money,
    weighted_midpoint,
)


class MoneyTests(SimpleTestCase):
    def test_string_decimals_quantized(self):
        self.assertEqual(to_money("0.1"), Decimal("0.100000"))
        self.assertEqual(to_money("1.2345675"), Decimal("1.234568"))  # half-even up
        self.assertEqual(to_money("1.2345685"), Decimal("1.234568"))  # half-even even
        self.assertEqual(to_money(0), Decimal("0.000000"))
        self.assertEqual(to_money(Decimal("2.5")), Decimal("2.500000"))

    def test_float_is_rejected(self):
        with self.assertRaises(ValueError):
            to_money(0.1)

    def test_negative_nan_overflow_rejected(self):
        for bad in ("-1", "NaN", "Infinity", "-Infinity", str(MAX_MONEY + 1)):
            with self.assertRaises(ValueError):
                to_money(bad)

    def test_weighted_midpoint_no_division_by_zero(self):
        self.assertEqual(weighted_midpoint(Decimal("1"), Decimal("0")), Decimal("0"))
        self.assertEqual(
            weighted_midpoint(Decimal("1"), Decimal("3")),
            Decimal("0.333333"),
        )


class StableBucketTests(SimpleTestCase):
    def test_deterministic(self):
        for _ in range(10):
            self.assertEqual(stable_bucket("exp_1", "user-x"), stable_bucket("exp_1", "user-x"))

    def test_distinct_experiments_can_split_differently(self):
        # Not asserting inequality (could coincide), but stability is key:
        # changing the experiment key changes the hash input deterministically.
        a = stable_bucket("exp_1", "user-x")
        b = stable_bucket("exp_2", "user-x")
        self.assertIn(a, {"A", "B"})
        self.assertIn(b, {"A", "B"})

    def test_balanced(self):
        counts = {"A": 0, "B": 0}
        for i in range(4000):
            counts[stable_bucket("exp_balance", f"u{i}")] += 1
        # Well within binomial noise (sd ~ 32) and away from biased splits.
        self.assertGreater(counts["A"], 1800)
        self.assertGreater(counts["B"], 1800)
