"""语法分析器单元测试：AST 结构、优先级、语句形式、错误定位。"""

import unittest

from taintflow import ast_nodes as ast
from taintflow.errors import ParseError
from taintflow.lexer import Lexer
from taintflow.parser import Parser


def parse(src):
    return Parser(Lexer(src).tokenize()).parse_program()


def parse_expr(src):
    prog = parse(f"fn main() {{ return {src}; }}")
    ret = prog.functions[0].body[0]
    assert isinstance(ret, ast.Return)
    return ret.value


class TestParser(unittest.TestCase):
    def test_minimal_program(self):
        prog = parse("fn main() {}")
        self.assertEqual(len(prog.functions), 1)
        self.assertEqual(prog.functions[0].name, "main")

    def test_params(self):
        prog = parse("fn add(a, b, c) { return a; }")
        self.assertEqual(prog.functions[0].params, ["a", "b", "c"])

    def test_duplicate_param_error(self):
        with self.assertRaises(ParseError):
            parse("fn f(a, a) {}")

    def test_assignment(self):
        prog = parse("fn main() { x = 5; }")
        stmt = prog.functions[0].body[0]
        self.assertIsInstance(stmt, ast.Assign)
        self.assertEqual(stmt.target, "x")

    def test_assignment_to_expression_error(self):
        with self.assertRaises(ParseError):
            parse("fn main() { f(x) = 1; }")

    def test_precedence(self):
        e = parse_expr("1 + 2 * 3")
        self.assertIsInstance(e, ast.Binary)
        self.assertEqual(e.op, "+")
        self.assertIsInstance(e.right, ast.Binary)
        self.assertEqual(e.right.op, "*")

    def test_precedence_comparison_vs_add(self):
        e = parse_expr("a + 1 < b + 2")
        self.assertEqual(e.op, "<")
        self.assertIsInstance(e.left, ast.Binary)

    def test_unary_and_call(self):
        e = parse_expr("not flag")
        self.assertIsInstance(e, ast.Unary)
        self.assertEqual(e.op, "!")
        e2 = parse_expr("-x")
        self.assertIsInstance(e2, ast.Unary)

    def test_call_args(self):
        e = parse_expr("f(1, g(2))")
        self.assertIsInstance(e, ast.Binary) if False else None
        self.assertIsInstance(e, ast.Call)
        self.assertEqual(e.name, "f")
        self.assertEqual(len(e.args), 2)
        self.assertIsInstance(e.args[1], ast.Call)
        self.assertEqual(e.args[1].name, "g")

    def test_if_else_structure(self):
        prog = parse("fn main() { if (c) { a; } else { b; } }")
        s = prog.functions[0].body[0]
        self.assertIsInstance(s, ast.If)
        self.assertEqual(len(s.then_body), 1)
        self.assertEqual(len(s.else_body), 1)

    def test_dangling_else_binds_nearest(self):
        # else 与内层 if 结合（内层 If 出现在外层 then_body 中）
        prog = parse("fn main() { if (a) { if (b) { x; } else { y; } } }")
        outer = prog.functions[0].body[0]
        self.assertEqual(len(outer.else_body), 0)
        inner = outer.then_body[0]
        self.assertEqual(len(inner.else_body), 1)

    def test_while_structure(self):
        prog = parse("fn main() { while (i < 3) { i = i + 1; } }")
        self.assertIsInstance(prog.functions[0].body[0], ast.While)

    def test_missing_semicolon(self):
        with self.assertRaises(ParseError):
            parse("fn main() { x = 1 }")

    def test_no_functions(self):
        with self.assertRaises(ParseError):
            parse("")

    def test_unclosed_paren(self):
        with self.assertRaises(ParseError):
            parse("fn main( { }")

    def test_every_node_has_span(self):
        prog = parse("fn main() { x = source(); sink(x); }")
        self.assertIsNotNone(prog.span)
        fn = prog.functions[0]
        self.assertIsNotNone(fn.name_span)
        self.assertIsNotNone(fn.body[0].span)

    def test_bool_literals(self):
        self.assertIsInstance(parse_expr("true"), ast.BoolLit)
        self.assertTrue(parse_expr("true").value)
        self.assertFalse(parse_expr("false").value)


if __name__ == "__main__":
    unittest.main()
