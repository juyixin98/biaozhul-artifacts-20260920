"""Lexer tests: tokens, keywords, comments, spans, illegal input."""

import unittest

from taintlang.errors import LexError
from taintlang.lexer import TokType, tokenize


class TestLexer(unittest.TestCase):
    def test_numbers_identifiers_keywords(self):
        toks = tokenize("func main 42 x_y")
        self.assertEqual([t.type for t in toks],
                         [TokType.FUNC, TokType.IDENT, TokType.NUMBER,
                          TokType.IDENT, TokType.EOF])
        self.assertEqual(toks[1].value, "main")
        self.assertEqual(toks[2].value, "42")

    def test_operators_two_char(self):
        toks = tokenize("== != <= >= && || = ! < > + - * / %")
        self.assertEqual(
            [t.type for t in toks],
            [TokType.EQ, TokType.NE, TokType.LE, TokType.GE, TokType.AND,
             TokType.OR, TokType.ASSIGN, TokType.BANG, TokType.LT, TokType.GT,
             TokType.PLUS, TokType.MINUS, TokType.STAR, TokType.SLASH,
             TokType.PERCENT, TokType.EOF])

    def test_string_and_bool(self):
        toks = tokenize('"a\\"b" true false')
        self.assertEqual([t.type for t in toks],
                         [TokType.STRING, TokType.TRUE, TokType.FALSE,
                          TokType.EOF])
        self.assertEqual(toks[0].value, '"a\\"b"')

    def test_comments_skipped(self):
        toks = tokenize("x // line comment\n /* block */ y")
        self.assertEqual([t.type for t in toks],
                         [TokType.IDENT, TokType.IDENT, TokType.EOF])
        self.assertEqual(toks[0].value, "x")
        self.assertEqual(toks[1].value, "y")

    def test_span_positions_1based(self):
        toks = tokenize("\n  abc")
        ident = next(t for t in toks if t.type is TokType.IDENT)
        self.assertEqual((ident.span.start.line, ident.span.start.column),
                         (2, 3))
        self.assertEqual(ident.span.text, "abc")

    def test_illegal_character(self):
        with self.assertRaises(LexError) as cm:
            tokenize("func @")
        self.assertIn("unexpected character", cm.exception.message)
        self.assertEqual(cm.exception.span.start.column, 6)

    def test_unterminated_string(self):
        with self.assertRaises(LexError):
            tokenize('x = "abc')

    def test_unterminated_block_comment(self):
        with self.assertRaises(LexError):
            tokenize("/* nope")


if __name__ == "__main__":
    unittest.main()
