"""Parser unit tests: grammar coverage and precise error locations."""

import unittest

from resflow.parser import parse_source, ParseError
from resflow import ast_nodes as ast
from tests._util import expect_error


def parse_one(src):
    fns = parse_source(src)
    return fns[0]


class TestParser(unittest.TestCase):

    def test_minimal_function(self):
        fn = parse_one("fn main() { return; }")
        self.assertEqual(fn.name, "main")
        self.assertEqual(fn.params, [])
        self.assertIsInstance(fn.body[0], ast.ReturnStmt)

    def test_params(self):
        fn = parse_one("fn f(a, b) { return; }")
        self.assertEqual(fn.params, ["a", "b"])

    def test_duplicate_params_rejected(self):
        with self.assertRaises(ParseError):
            parse_one("fn f(a, a) { return; }")

    def test_resource_statements_both_styles(self):
        fn = parse_one("""fn f() {
          acquire(a);
          release (b);
          use (c);
        }""")
        ops = [type(s) for s in fn.body]
        self.assertEqual(ops, [ast.Acquire, ast.Release, ast.Use])
        self.assertEqual(fn.body[0].resource, "a")

    def test_throw_requires_string(self):
        with self.assertRaises(ParseError):
            parse_one("fn f(){ throw 1; }")

    def test_if_else_and_while_and_try(self):
        fn = parse_one("""fn f() {
          if (x) { acquire(a); } else { release(a); }
          while (y) { use(a); }
          try { throw "z"; } catch (e) { release(a); }
        }""")
        self.assertIsInstance(fn.body[0], ast.IfStmt)
        self.assertIsInstance(fn.body[1], ast.WhileStmt)
        self.assertIsInstance(fn.body[2], ast.TryStmt)
        self.assertEqual(fn.body[2].message, "e")
        self.assertIsInstance(fn.body[2].body[0], ast.ThrowStmt)

    def test_let_with_and_without_initializer(self):
        fn = parse_one("fn f(){ let a; let b = 1; }")
        self.assertIsNone(fn.body[0].init)
        self.assertIsInstance(fn.body[1].init, ast.IntLit)

    def test_expression_precedence(self):
        fn = parse_one("fn f(){ let x = 1 + 2 * 3; }")
        e = fn.body[0].init
        self.assertIsInstance(e, ast.Binary)
        self.assertEqual(e.op, "+")
        self.assertIsInstance(e.right, ast.Binary)
        self.assertEqual(e.right.op, "*")

    def test_comparison_and_logical(self):
        fn = parse_one("fn f(){ let x = a < 2 && b == 3 || c; }")
        e = fn.body[0].init
        self.assertEqual(e.op, "||")

    def test_unary_and_parens(self):
        fn = parse_one("fn f(){ let x = !(a && b); let y = -(1); }")
        self.assertIsInstance(fn.body[0].init, ast.Unary)
        self.assertEqual(fn.body[0].init.op, "!")

    def test_assignment(self):
        fn = parse_one("fn f(){ let a = 1; a = 2; }")
        self.assertIsInstance(fn.body[1], ast.Assign)
        self.assertEqual(fn.body[1].target, "a")

    def test_empty_program_rejected(self):
        with self.assertRaises(ParseError):
            parse_source("// only a comment")

    def test_duplicate_function_rejected(self):
        with self.assertRaises(ParseError):
            parse_source("fn a(){return;} fn a(){return;}")

    def test_missing_semicolon_location(self):
        with self.assertRaises(ParseError) as cm:
            parse_one("fn f(){ acquire(a) }")
        self.assertEqual(cm.exception.location.line, 1)

    def test_unclosed_brace_location(self):
        with self.assertRaises(ParseError):
            parse_one("fn f(){ ")

    def test_unexpected_token(self):
        with self.assertRaises(ParseError):
            parse_one("fn f(){ 123; }")

    def test_source_position_preserved_on_nodes(self):
        fn = parse_one("fn f() {\n  acquire(r);\n}")
        acq = fn.body[0]
        self.assertEqual(acq.loc.line, 2)
        self.assertEqual(acq.loc.column, 3)

    def test_const_fold_basic(self):
        self.assertEqual(ast.const_eval(parse_one(
            "fn f(){ if (1 < 2) {} }").body[0].cond), True)
        self.assertEqual(ast.const_eval(parse_one(
            "fn f(){ while (false) {} }").body[0].cond), False)
        self.assertIsNone(ast.const_eval(parse_one(
            "fn f(){ if (x) {} }").body[0].cond))

    def test_const_fold_arithmetic(self):
        fn = parse_one("fn f(){ let x = 2 + 3 * 4; }")
        self.assertEqual(ast.const_eval(fn.body[0].init), 14)


if __name__ == "__main__":
    unittest.main()
