import unittest

from lattlang.semantics import (
    ZeroDivisionTrapped,
    apply_binary,
    apply_unary,
    trunc_div,
    trunc_mod,
)


class TestSemantics(unittest.TestCase):
    def test_truncation_toward_zero(self):
        self.assertEqual(trunc_div(-7, 2), -3)
        self.assertEqual(trunc_div(7, -2), -3)
        self.assertEqual(trunc_div(-7, -2), 3)
        self.assertEqual(trunc_mod(-7, 2), -1)
        self.assertEqual(trunc_mod(7, -2), 1)
        self.assertEqual(trunc_mod(-7, -2), -1)

    def test_division_identity(self):
        for a, b in [(-7, 2), (7, -2), (-7, -2), (8, 3), (-8, 3)]:
            self.assertEqual(a, trunc_div(a, b) * b + trunc_mod(a, b))

    def test_divide_by_zero(self):
        with self.assertRaises(ZeroDivisionTrapped):
            trunc_div(1, 0)
        with self.assertRaises(ZeroDivisionTrapped):
            trunc_mod(1, 0)

    def test_comparisons_are_int(self):
        self.assertEqual(apply_binary("lt", 2, 3), 1)
        self.assertEqual(apply_binary("ge", 3, 3), 1)
        self.assertEqual(apply_binary("eq", 1, 2), 0)

    def test_logical_ops_non_short_circuit_values(self):
        self.assertEqual(apply_binary("and", 1, 1), 1)
        self.assertEqual(apply_binary("and", 1, 0), 0)
        self.assertEqual(apply_binary("or", 0, 0), 0)
        self.assertEqual(apply_binary("or", 0, 2), 1)

    def test_unary(self):
        self.assertEqual(apply_unary("neg", 5), -5)
        self.assertEqual(apply_unary("not", 0), 1)
        self.assertEqual(apply_unary("not", 3), 0)


if __name__ == "__main__":
    unittest.main()
