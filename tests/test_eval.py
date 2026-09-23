"""求值器行为测试。"""
from __future__ import annotations

import unittest

from tests.helpers import analyze_ok, analyze_err


class EvalTests(unittest.TestCase):
    def test_arith(self) -> None:
        self.assertEqual(analyze_ok("7 / 2")["value"], "3")
        self.assertEqual(analyze_ok("10 - 3 * 2")["value"], "4")

    def test_division_by_zero(self) -> None:
        payload = analyze_ok("1 / 0")
        self.assertIsNotNone(payload["eval_error"])
        self.assertEqual(payload["eval_error"]["kind"], "RuntimeFailure")

    def test_closures_and_shadowing(self) -> None:
        src = "let add = fun x -> fun y -> x + y in add 3 4"
        self.assertEqual(analyze_ok(src)["value"], "7")

    def test_recursion(self) -> None:
        src = (
            "let rec fib n = "
            "if n <= 1 then n else fib (n - 1) + fib (n - 2) "
            "in fib 10"
        )
        self.assertEqual(analyze_ok(src)["value"], "55")

    def test_ref_cell_mutation(self) -> None:
        src = (
            "let acc = ref 0 in "
            "let rec bump n = if n <= 0 then deref acc "
            "else (acc <- deref acc + n; bump (n - 1)) "
            "in bump 5"
        )
        self.assertEqual(analyze_ok(src)["value"], "15")

    def test_conditional_short_circuit_not_needed(self) -> None:
        src = "if not false then 1 else 2"
        self.assertEqual(analyze_ok(src)["value"], "1")

    def test_type_error_precedes_eval(self) -> None:
        # 类型错误时不求值：错误响应而非 eval_error
        err = analyze_err("true + 1")
        self.assertEqual(err["kind"], "UnifyError")
        self.assertIn("bool", err["message"])


if __name__ == "__main__":
    unittest.main()
