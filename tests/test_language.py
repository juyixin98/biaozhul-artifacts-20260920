"""Tests for lexer/parser/resolver and the reference interpreter."""

import unittest

from intervalai import execute_source
from intervalai.errors import ConcreteExecError, ParseError, SemanticError
from intervalai.pipeline import parse_program


class TestParser(unittest.TestCase):
    def test_basic_program(self):
        prog = parse_program(
            "input n;\narr a[2] = [1, 2];\nvar x = 0;\nx = a[0] + n * 2;\n")
        self.assertEqual(len(prog.arr_decls), 1)
        self.assertEqual(prog.arr_decls[0].size, 2)

    def test_keyword_and_symbol_operators(self):
        parse_program("input a; input b; var x = 0;"
                      "if (a > 0 and b > 0 or not (a == 0)) { x = a; }")
        parse_program("input a; input b; var x = 0;"
                      "if (a > 0 && b > 0 || !(a == 0)) { x = a; }")

    def test_locations_preserved(self):
        src = "input n;\nvar x = 0;\nx = n + 1;\n"
        prog = parse_program(src)
        assign = prog.body[0]
        self.assertEqual(assign.loc.line, 3)
        self.assertEqual(prog.var_decls[0].loc.line, 1)

    def test_errors(self):
        with self.assertRaises(ParseError):
            parse_program("input n;\nvar x = ;\n")
        with self.assertRaises(SemanticError):
            parse_program("var x = 0;\nvar x = 1;\n")
        with self.assertRaises(SemanticError):
            parse_program("var x = 0;\nx = y;\n")
        with self.assertRaises(SemanticError):
            parse_program("arr a[2] = [1];\n")
        with self.assertRaises(SemanticError):
            parse_program("var x = 1;\nx[0] = 2;\n")
        with self.assertRaises(SemanticError):
            parse_program("input a;\nif (a + 1) { a = 0; }\n")

    def test_large_integer(self):
        prog = parse_program("var x = 99999999999999999999999999;\n")
        self.assertEqual(prog.var_decls[0].init,
                         99999999999999999999999999)


class TestConcrete(unittest.TestCase):
    def test_loop_and_array(self):
        out = execute_source(
            "input n;\narr a[5]=[0,0,0,0,0];\nvar i=0;\n"
            "while (i < n) { a[i] = i*i; i = i + 1; }\n",
            {"n": 4})
        self.assertEqual(out["arrays"]["a"], [0, 1, 4, 9, 0])
        self.assertEqual(out["scalars"]["i"], 4)

    def test_floor_division(self):
        # Mathematical-integer floor semantics incl. negatives
        for a, b, q in [(-7, 2, -4), (7, -2, -4), (-7, -2, 3), (7, 2, 3)]:
            out = execute_source(f"input a; input b; var q = a / b;",
                                 {"a": a, "b": b})
            self.assertEqual(out["scalars"]["q"], q, f"{a}//{b}")

    def test_div_zero(self):
        with self.assertRaises(ConcreteExecError) as cm:
            execute_source("var z = 0;\nvar q = 1 / z;\n")
        self.assertEqual(cm.exception.kind, "div_by_zero")
        self.assertEqual(cm.exception.loc.line, 2)

    def test_negative_index_is_oob(self):
        with self.assertRaises(ConcreteExecError) as cm:
            execute_source("arr a[3] = [1,2,3];\nvar x = a[-1];\n")
        self.assertEqual(cm.exception.kind, "index_out_of_bounds")

    def test_step_limit(self):
        with self.assertRaises(ConcreteExecError):
            execute_source("while (true) { }", step_limit=100)


if __name__ == "__main__":
    unittest.main()
