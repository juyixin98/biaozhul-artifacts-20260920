"""词法分析器单元测试：位置、字符串转义、注释、错误。"""

import unittest

from taintflow.errors import LexError
from taintflow.lexer import Lexer


def lex(src):
    return [(t.kind, t.value) for t in Lexer(src).tokenize() if t.kind != "eof"]


class TestLexer(unittest.TestCase):
    def test_numbers_idents_keywords(self):
        toks = lex("fn main() { x = 42; return true; }")
        kinds = [k for k, _ in toks]
        self.assertIn("keyword", kinds)
        self.assertEqual(lex("42")[0], ("number", 42))
        self.assertEqual(lex("foo_bar1")[0], ("ident", "foo_bar1"))

    def test_operators_multichar(self):
        toks = [t for t in Lexer("a == b != c <= d >= e && f || g").tokenize()
                if t.kind == "op"]
        vals = [t.value for t in toks]
        self.assertEqual(vals, ["==", "!=", "<=", ">=", "&&", "||"])

    def test_string_escapes(self):
        self.assertEqual(lex('"a\\nb"')[0], ("string", "a\nb"))
        self.assertEqual(lex("'it\\''")[0], ("string", "it'"))

    def test_comments(self):
        toks = lex("x = 1; // line comment\n y = 2; /* block */ z = 3;")
        idents = [v for k, v in toks if k == "ident"]
        self.assertEqual(idents, ["x", "y", "z"])

    def test_nested_block_comments(self):
        toks = lex("/* outer /* inner */ still */ x;")
        self.assertEqual([v for k, v in toks if k == "ident"], ["x"])

    def test_position_tracking(self):
        toks = Lexer("a = 1;\nb = 2;").tokenize()
        ident_b = next(t for t in toks if t.kind == "ident" and t.value == "b")
        self.assertEqual((ident_b.span.start.line, ident_b.span.start.col), (2, 1))

    def test_unterminated_string(self):
        with self.assertRaises(LexError):
            lex('"abc')

    def test_illegal_char(self):
        with self.assertRaises(LexError):
            lex("@")

    def test_unclosed_block_comment(self):
        with self.assertRaises(LexError):
            lex("/* never closes")


if __name__ == "__main__":
    unittest.main()
