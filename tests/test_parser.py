import unittest

from lattlang import ast_nodes as ast
from lattlang.errors import LangError
from lattlang.parser import parse


class TestParser(unittest.TestCase):
    def test_minimal_program(self):
        p = parse("print 1;")
        self.assertEqual(len(p.body), 1)
        self.assertIsInstance(p.body[0], ast.Print)

    def test_assign_optional_semicolon(self):
        self.assertEqual(len(parse("x := 1").body), 1)
        self.assertEqual(len(parse("x := 1;").body), 1)

    def test_precedence(self):
        # 1 + 2 * 3 parses as 1 + (2*3)
        p = parse("print 1 + 2 * 3")
        b = p.body[0].value
        self.assertIsInstance(b, ast.Binary)
        self.assertEqual(b.op, "+")
        self.assertIsInstance(b.right, ast.Binary)
        self.assertEqual(b.right.op, "*")

    def test_parenthesized(self):
        p = parse("print (1 + 2) * 3")
        b = p.body[0].value
        self.assertEqual(b.op, "*")
        self.assertIsInstance(b.left, ast.Binary)
        self.assertEqual(b.left.op, "+")

    def test_unary_chain(self):
        p = parse("print !!-0")
        e = p.body[0].value
        self.assertIsInstance(e, ast.Unary)
        self.assertEqual(e.op, "!")

    def test_if_else_if(self):
        p = parse("if 1 { print 1; } else if 0 { print 2; } else { print 3; }")
        self.assertIsInstance(p.body[0], ast.If)
        self.assertEqual(len(p.body[0].else_), 1)
        self.assertIsInstance(p.body[0].else_[0], ast.If)

    def test_while_with_block(self):
        p = parse("while 1 { x := x + 1; }")
        self.assertIsInstance(p.body[0], ast.While)

    def test_span_covers_full_assignment(self):
        p = parse("x := 1 + 2")
        s = p.body[0].span
        self.assertEqual((s.start_line, s.start_col), (1, 1))
        self.assertEqual((s.end_line, s.end_col), (1, 11))

    def test_missing_expression(self):
        with self.assertRaises(LangError) as cm:
            parse("x :=")
        self.assertEqual(cm.exception.stage, "parse")

    def test_unterminated_block(self):
        with self.assertRaises(LangError):
            parse("if 1 { print 1;")

    def test_boolean_literals(self):
        p = parse("print true || false")
        self.assertIsInstance(p.body[0].value.left, ast.BoolLit)


if __name__ == "__main__":
    unittest.main()
