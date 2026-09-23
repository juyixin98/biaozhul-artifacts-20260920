"""Lexer, parser and source-position tests."""
import unittest

from sclang.errors import CompileError
from sclang.lexer import Lexer, TokenKind
from sclang.parser import parse_source


class TestLexer(unittest.TestCase):
    def test_tokens_and_spans(self):
        toks = Lexer("let x = 42;").tokenize()
        kinds = [t.kind for t in toks]
        self.assertEqual(kinds, [TokenKind.LET, TokenKind.IDENT,
                                 TokenKind.ASSIGN, TokenKind.INT,
                                 TokenKind.SEMICOLON, TokenKind.EOF])
        self.assertEqual(toks[3].value, 42)
        # integer literal location (1-based)
        s = toks[3].span
        self.assertEqual((s.line, s.col, s.end_col), (1, 9, 11))

    def test_multiline_lines_and_cols(self):
        toks = Lexer("a\n  b").tokenize()
        ident_b = [t for t in toks if t.kind == TokenKind.IDENT and
                   t.value == "b"][0]
        self.assertEqual((ident_b.span.line, ident_b.span.col), (2, 3))

    def test_comments_skipped(self):
        toks = Lexer("1 + 2 # a comment\n + 3").tokenize()
        nums = [t.value for t in toks if t.kind == TokenKind.INT]
        self.assertEqual(nums, [1, 2, 3])

    def test_string_escapes(self):
        toks = Lexer(r'"a\nb\tc\\d"').tokenize()
        self.assertEqual(toks[0].value, "a\nb\tc\\d")

    def test_unterminated_string(self):
        with self.assertRaises(CompileError) as ctx:
            Lexer('"abc').tokenize()
        self.assertEqual(ctx.exception.span.line, 1)

    def test_trailing_two_char_operator_at_eof(self):
        # must not read past end of input
        toks = Lexer("=").tokenize()
        self.assertEqual(toks[0].kind, TokenKind.ASSIGN)

    def test_illegal_char(self):
        with self.assertRaises(CompileError):
            Lexer("@").tokenize()


class TestParser(unittest.TestCase):
    def test_associativity_and_precedence(self):
        from sclang import ast_nodes as ast
        p = parse_source("1 - 2 - 3;")
        bin_ = p.body[0].expr
        self.assertIsInstance(bin_, ast.Binary)
        self.assertEqual(bin_.op, "-")
        self.assertIsInstance(bin_.left, ast.Binary)  # left-assoc
        self.assertEqual(bin_.right.value, 3)

        p = parse_source("1 + 2 * 3;")
        b = p.body[0].expr
        self.assertEqual(b.op, "+")
        self.assertEqual(b.right.op, "*")

    def test_nested_function_parse(self):
        p = parse_source("fn outer(x){ fn inner(y){ return x+y; } }")
        outer = p.body[0]
        self.assertEqual(outer.name, "outer")
        self.assertEqual(outer.params, ["x"])
        inner = outer.body.body[0]
        self.assertEqual(inner.name, "inner")

    def test_lambda_and_call(self):
        from sclang import ast_nodes as ast
        p = parse_source("let f = fn(a){ return a; }; f(1);")
        self.assertIsInstance(p.body[0].init, ast.FunExpr)
        self.assertIsInstance(p.body[1].expr, ast.Call)

    def test_error_reports_position(self):
        with self.assertRaises(CompileError) as ctx:
            parse_source("let = 1;")
        self.assertTrue(ctx.exception.span is not None)

    def test_if_else_nesting(self):
        p = parse_source("if(1){0;} else { if(2){1;} }")
        self.assertIsNotNone(p.body[0].otherwise)

    def test_duplicate_params(self):
        with self.assertRaises(CompileError):
            parse_source("fn f(a,a){}")


if __name__ == "__main__":
    unittest.main()
