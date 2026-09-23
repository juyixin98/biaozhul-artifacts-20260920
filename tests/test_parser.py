"""词法/语法分析与源码位置测试。"""

import unittest

from constprop.lexer import Lexer
from constprop.parser import parse_source
from constprop.source import LexError, ParseError, SourceText


def lex(text):
    return Lexer(SourceText(text)).tokenize()


class TestLexer(unittest.TestCase):
    def test_basic_tokens(self):
        toks = lex("x = 10;")
        self.assertEqual((toks[0].kind, toks[0].text), ("IDENT", "x"))
        self.assertEqual((toks[1].kind, toks[1].text), ("PUNCT", "="))
        self.assertEqual((toks[2].kind, toks[2].text), ("INT", "10"))
        self.assertEqual(toks[-1].kind, "EOF")

    def test_keywords_and_multichar(self):
        toks = lex("if while print true false == != <= >= && ||")
        words = [t.text for t in toks if t.kind == "KEYWORD"]
        self.assertEqual(words,
                         ["if", "while", "print", "true", "false"])
        ops = [t.text for t in lex("a == b != c <= d >= e && f || g")
               if t.text in ("==", "!=", "<=", ">=", "&&", "||")]
        self.assertEqual(ops, ["==", "!=", "<=", ">=", "&&", "||"])

    def test_comment_to_end_of_line(self):
        toks = lex("x = 1; // a comment\ny = 2;")
        idents = [t.text for t in toks if t.kind == "IDENT"]
        self.assertEqual(idents, ["x", "y"])

    def test_illegal_char(self):
        with self.assertRaises(LexError) as ctx:
            lex("x = @;")
        self.assertEqual(ctx.exception.span.start_col, 5)

    def test_offsets_tracked(self):
        toks = lex("ab\n cd")
        # cd 起始偏移为 4
        cd = next(t for t in toks if t.text == "cd")
        self.assertEqual(cd.start, 4)


class TestParser(unittest.TestCase):
    def _parse(self, text):
        return parse_source(SourceText(text))

    def test_empty_program(self):
        p = self._parse("")
        self.assertEqual(p.body, [])
        p2 = self._parse("   // only comment\n")
        self.assertEqual(p2.body, [])

    def test_assignment_precedence(self):
        from constprop import ast as A
        p = self._parse("x = 1 + 2 * 3;")
        e = p.body[0].value
        self.assertIsInstance(e, A.Binary)
        self.assertEqual(e.op, "+")
        self.assertIsInstance(e.right, A.Binary)
        self.assertEqual(e.right.op, "*")

    def test_comparison_non_assoc_single(self):
        from constprop import ast as A
        p = self._parse("x = 1 < 2;")
        self.assertIsInstance(p.body[0].value, A.Binary)
        # 比较不允许连续：1 < 2 < 3 中第二个 < 处应停止，随后 ';' 报错
        with self.assertRaises(ParseError):
            self._parse("x = 1 < 2 < 3;")

    def test_unary_and_logical(self):
        from constprop import ast as A
        p = self._parse("x = !a && -b || c;")
        self.assertIsInstance(p.body[0].value, A.Logical)  # ||
        self.assertIsInstance(p.body[0].value.left, A.Logical)  # &&

    def test_if_else_and_while(self):
        from constprop import ast as A
        p = self._parse("if (a) print 1; else print 2;")
        self.assertIsInstance(p.body[0], A.If)
        self.assertIsNotNone(p.body[0].otherwise)
        p2 = self._parse("while (x) { x = x - 1; }")
        self.assertIsInstance(p2.body[0], A.While)
        self.assertIsInstance(p2.body[0].body, A.Block)

    def test_missing_semicolon(self):
        with self.assertRaises(ParseError) as ctx:
            self._parse("x = 1")
        # 报错位置应指向 EOF
        self.assertIn("';'", ctx.exception.message)

    def test_unexpected_keyword_as_stmt(self):
        with self.assertRaises(ParseError):
            self._parse("true;")

    def test_spans_present_on_nodes(self):
        p = self._parse("x = 1 +\n 2;")
        span = p.body[0].span
        self.assertEqual(span.start_line, 1)
        self.assertEqual(span.end_line, 2)


if __name__ == "__main__":
    unittest.main()
