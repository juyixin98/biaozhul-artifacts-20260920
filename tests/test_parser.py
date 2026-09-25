"""Tests for the parser: structure, precedence and source spans."""

import unittest

from miniml.parser import ParseError, parse
from miniml.ast_nodes import (
    App,
    Assign,
    BinOp,
    BoolLit,
    If,
    IntLit,
    Lam,
    Let,
    Ref,
    Seq,
)


def parse_one(src: str):
    p = parse(src)
    assert p.final_expr is not None
    return p.final_expr


class TestParserStructure(unittest.TestCase):
    def test_top_level_bindings_and_final_expr(self):
        p = parse("let x = 1 ;;\nlet y = 2 ;;\nx + y")
        self.assertEqual([b.name for b in p.bindings], ["x", "y"])
        self.assertIsInstance(p.final_expr, BinOp)

    def test_inline_let_expression_at_top_level(self):
        e = parse_one("let x = 1 in x + 1")
        self.assertIsInstance(e, Let)
        self.assertFalse(e.rec)

    def test_rec_only_binds_functions(self):
        with self.assertRaises(ParseError):
            parse("let rec x = 1 ;;")
        # function rec is fine
        parse("let rec f = fun x -> f x ;;")

    def test_lambda_single_param(self):
        e = parse_one("fun x -> x + 1")
        self.assertIsInstance(e, Lam)
        self.assertEqual(e.param, "x")


class TestPrecedence(unittest.TestCase):
    def test_arith_precedence_and_associativity(self):
        e = parse_one("1 + 2 * 3")
        # 1 + (2 * 3)
        self.assertEqual(e.op, "+")
        self.assertIsInstance(e.left, IntLit)
        self.assertEqual(e.right.op, "*")

    e_left = None

    def test_subtraction_left_assoc(self):
        e = parse_one("10 - 2 - 3")
        self.assertEqual(e.op, "-")
        self.assertEqual(e.left.op, "-")  # (10 - 2) - 3

    def test_comparison_non_associative(self):
        with self.assertRaises(ParseError):
            parse_one("1 < 2 < 3")

    def test_assignment_right_assoc(self):
        e = parse_one("ref 1 := ref 2 := 3")
        self.assertIsInstance(e, Assign)
        self.assertIsInstance(e.value, Assign)

    def test_application_binds_tighter_than_arithmetic(self):
        e = parse_one("f 1 + g 2")
        self.assertEqual(e.op, "+")
        self.assertIsInstance(e.left, App)
        self.assertIsInstance(e.right, App)

    def test_prefix_ref_and_deref(self):
        e = parse_one("!r + 1")
        self.assertEqual(e.op, "+")
        self.assertEqual(e.left.inner.name, "r")

    def test_if_else_parity(self):
        e = parse_one("if true then 1 else 2")
        self.assertIsInstance(e, If)
        self.assertIsNotNone(e.els)

    def test_sequence(self):
        e = parse_one("1 ; 2 ; 3")
        self.assertIsInstance(e, Seq)
        self.assertIsInstance(e.first, Seq)


class TestSpans(unittest.TestCase):
    def test_every_node_carries_a_span(self):
        src = "(fun x -> x + 1) (2 * 3)"
        e = parse_one(src)
        self.assertEqual((e.span.start.line, e.span.start.col), (1, 1))
        # span covers the whole application including the closing parenthesis
        self.assertEqual(e.span.hi, len(src))
        self.assertEqual(e.span.end.col, len(src) + 1)

    def test_identifier_span_points_exactly(self):
        e = parse_one("   abc")
        self.assertEqual((e.span.start.col, e.span.end.col), (4, 7))


class TestParseErrors(unittest.TestCase):
    def test_unclosed_paren(self):
        with self.assertRaises(ParseError) as cm:
            parse("(1 + 2")
        self.assertEqual(cm.exception.span.start.line, 1)

    def test_top_level_requires_separator(self):
        with self.assertRaises(ParseError):
            parse("let x = 1 let y = 2 ;;")


if __name__ == "__main__":
    unittest.main()
