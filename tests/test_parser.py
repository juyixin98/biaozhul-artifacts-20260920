"""Tests for the lexer + recursive-descent parser."""
from __future__ import annotations

import unittest

from ssa_toolchain import ast_nodes as ast
from ssa_toolchain.errors import LexError, ParseError
from ssa_toolchain.parser import parse


class LexerTests(unittest.TestCase):
    def test_operators_and_numbers(self):
        prog = parse("func f(a) { var x = 0xFF + 12; return x; }")
        f = prog.funcs[0]
        self.assertEqual(f.name, "f")
        self.assertEqual(f.params, ("a",))

    def test_line_and_block_comments(self):
        src = "// hi\nfunc f() { /* block */ return 1; }"
        self.assertEqual(parse(src).funcs[0].name, "f")

    def test_unterminated_block_comment(self):
        with self.assertRaises(LexError):
            parse("func f() { /* no end }")

    def test_bad_character(self):
        with self.assertRaises(LexError):
            parse("func f() { var x = $; }")

    def test_locations_are_1_based(self):
        src = "func f() {\n  return 42;\n}\n"
        f = parse(src).funcs[0]
        ret = f.body[0]
        self.assertIsInstance(ret, ast.Return)
        self.assertEqual((ret.loc.line, ret.loc.col), (2, 3))
        self.assertEqual(ret.value.value, 42)


class ParserTests(unittest.TestCase):
    def _first_func(self, src):
        return parse(src).funcs[0]

    def test_precedence(self):
        # 1 + 2 * 3  vs  (1 + 2) * 3
        f = self._first_func("func f() { var x = 1 + 2 * 3; return x; }")
        e = f.body[0].init
        self.assertIsInstance(e, ast.Binary)
        self.assertEqual(e.op, "+")
        self.assertIsInstance(e.right, ast.Binary)
        self.assertEqual(e.right.op, "*")

    def test_left_associative(self):
        f = self._first_func("func f() { var x = 1 - 2 - 3; return x; }")
        e = f.body[0].init
        self.assertEqual(e.op, "-")
        self.assertIsInstance(e.left, ast.Binary)
        self.assertEqual(e.left.op, "-")

    def test_unary_and_comparison(self):
        f = self._first_func("func f() { var x = !!(0 - 5 < 3); return x; }")
        self.assertIsInstance(f.body[0].init, ast.Unary)

    def test_duplicate_param(self):
        with self.assertRaises(ParseError):
            parse("func f(a, a) { return a; }")

    def test_duplicate_decl(self):
        with self.assertRaises(ParseError):
            parse("func f() { var x = 1; var x = 2; return x; }")

    def test_reserved_prefix(self):
        with self.assertRaises(ParseError):
            parse("func f() { var __x = 1; return __x; }")

    def test_missing_semicolon(self):
        with self.assertRaises(ParseError):
            parse("func f() { var x = 1 return x; }")

    def test_call_parsing(self):
        f = self._first_func("func f() { var x = g(1, 2); return x; }")
        call = f.body[0].init
        self.assertIsInstance(call, ast.Call)
        self.assertEqual(call.name, "g")
        self.assertEqual(len(call.args), 2)


if __name__ == "__main__":
    unittest.main()
