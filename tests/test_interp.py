"""Interpreter semantics: integer ops, short-circuit, calls, control flow."""
from __future__ import annotations

import unittest

from .helpers import compile_source, load_example


class ArithmeticTests(unittest.TestCase):
    def run_expr(self, expr, *inputs):
        src = f"func main(a) {{ var r = {expr}; return r; }}"
        res = compile_source(src, inputs=list(inputs))
        ex = res.executions["main"]
        self.assertTrue(ex["agree"])
        return ex["flat"]["return"]

    def test_truncated_division_toward_zero(self):
        self.assertEqual(self.run_expr("a / 2", 7), 3)
        self.assertEqual(self.run_expr("a / 2", -7), -3)
        self.assertEqual(self.run_expr("a / -2", 7), -3)

    def test_remainder_sign_of_dividend(self):
        self.assertEqual(self.run_expr("a % 3", 7), 1)
        self.assertEqual(self.run_expr("a % 3", -7), -1)
        self.assertEqual(self.run_expr("a % -3", 7), 1)

    def test_bitwise_and_shifts(self):
        self.assertEqual(self.run_expr("(a << 2) & 15", 1), 4)
        self.assertEqual(self.run_expr("a ^ 3 | a", 5), 7)

    def test_comparisons_are_zero_or_one(self):
        self.assertEqual(self.run_expr("a < 2", 1), 1)
        self.assertEqual(self.run_expr("a < 1", 2), 0)

    def test_unary(self):
        self.assertEqual(self.run_expr("-a", 5), -5)
        self.assertEqual(self.run_expr("~a", 0), -1)
        self.assertEqual(self.run_expr("!a", 0), 1)
        self.assertEqual(self.run_expr("!a", 9), 0)


class ShortCircuitTests(unittest.TestCase):
    def test_and_does_not_observe_divide_by_zero(self):
        src = """
        func main(a) {
          var r = (a != 0) && (10 / a > 1);
          return r;
        }
        """
        res = compile_source(src, inputs=[0])
        self.assertEqual(res.executions["main"]["flat"]["return"], 0)
        res = compile_source(src, inputs=[2])
        self.assertEqual(res.executions["main"]["flat"]["return"], 1)

    def test_or_short_circuit(self):
        src = """
        func main(a) {
          var r = (a == 0) || (10 / a > 1);
          return r;
        }
        """
        self.assertEqual(compile_source(src, inputs=[0])
                         .executions["main"]["flat"]["return"], 1)


class FunctionCallTests(unittest.TestCase):
    SRC = """
    func square(x) { return x * x; }
    func main(n) {
      return square(n) + square(n + 1);
    }
    """

    def test_call_returns_and_args(self):
        res = compile_source(self.SRC, inputs=[4])
        ex = res.executions
        self.assertEqual(ex["main"]["flat"]["return"], 16 + 25)
        self.assertTrue(ex["main"]["agree"])


class UnreachableStatementTests(unittest.TestCase):
    def test_code_after_return_dropped(self):
        src = """
        func main() {
          return 1;
          var x = 2;   // never executed, must not affect anything
        }
        """
        res = compile_source(src, inputs=[])
        self.assertEqual(res.executions["main"]["flat"]["return"], 1)


class PrintTests(unittest.TestCase):
    def test_prints_are_captured(self):
        res = compile_source(load_example("diamond.mini"), inputs=[-4])
        out = res.executions["main"]
        self.assertTrue(out["agree"])
        self.assertEqual(out["flat"]["printed"], [-5])


if __name__ == "__main__":
    unittest.main()
