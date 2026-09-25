"""Parser tests: grammar coverage, precedence and spans."""

import unittest

from taintlang import ast_nodes as ast
from taintlang.errors import ParseError
from taintlang.lexer import tokenize
from taintlang.parser import Parser, parse


def parse_expr(src):
    # parse a standalone expression via a wrapper function body
    program = parse(f"func f() {{ var __t = {src}; }}")
    decl = program.functions[0].body.statements[0]
    return decl.init


class TestParser(unittest.TestCase):
    def test_empty_program_rejected(self):
        with self.assertRaises(ParseError):
            parse("")

    def test_function_params(self):
        p = parse("func add(a, b) { return a + b; }")
        f = p.functions[0]
        self.assertEqual(f.name, "add")
        self.assertEqual(f.params, ("a", "b"))

    def test_duplicate_parameter(self):
        with self.assertRaises(ParseError):
            parse("func f(a, a) { return a; }")

    def test_precedence_climbing(self):
        e = parse_expr("1 + 2 * 3")
        self.assertIsInstance(e, ast.Binary)
        self.assertEqual(e.op, "+")
        self.assertIsInstance(e.right, ast.Binary)
        self.assertEqual(e.right.op, "*")

    def test_parenthesized_precedence(self):
        e = parse_expr("(1 + 2) * 3")
        self.assertIsInstance(e, ast.Binary)
        self.assertEqual(e.op, "*")
        self.assertIsInstance(e.left, ast.Binary)

    def test_unary_binding(self):
        e = parse_expr("!a && -b")
        self.assertEqual(e.op, "&&")
        self.assertIsInstance(e.left, ast.Unary)
        self.assertIsInstance(e.right, ast.Unary)

    def test_if_else_chain(self):
        p = parse("func f() { if (a) { b; } else { c; } }")
        if_stmt = p.functions[0].body.statements[0]
        self.assertIsInstance(if_stmt, ast.IfStmt)
        self.assertIsNotNone(if_stmt.else_block)
        self.assertEqual(len(if_stmt.then_block.statements), 1)

    def test_while_and_return(self):
        p = parse("func f() { while (i < 3) { i = i + 1; } return; }")
        stmts = p.functions[0].body.statements
        self.assertIsInstance(stmts[0], ast.WhileStmt)
        self.assertIsInstance(stmts[1], ast.ReturnStmt)
        self.assertIsNone(stmts[1].value)

    def test_call_parsing(self):
        e = parse_expr("f(1, g(2), 3)")
        self.assertIsInstance(e, ast.Call)
        self.assertEqual(e.callee, "f")
        self.assertEqual(len(e.args), 3)
        self.assertIsInstance(e.args[1], ast.Call)

    def test_missing_semicolon(self):
        with self.assertRaises(ParseError) as cm:
            parse("func f() { var x = 1 }")
        self.assertIn("';'", cm.exception.message)

    def test_undeclared_is_not_a_parse_error(self):
        # declaration checking happens in the builder, not the parser
        p = parse("func f() { sink(y); }")
        self.assertEqual(p.functions[0].name, "f")

    def test_node_spans_present(self):
        p = parse("func f() { return 42; }")
        ret = p.functions[0].body.statements[0]
        self.assertGreaterEqual(ret.span.start.line, 1)
        self.assertEqual(ret.span.text, "")  # statements do not slice text
        num = ret.value
        self.assertEqual(num.span.text, "42")


if __name__ == "__main__":
    unittest.main()
