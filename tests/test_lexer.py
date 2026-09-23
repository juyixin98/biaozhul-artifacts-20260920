"""Tests for the lexer: tokens, escapes, classes, quantifiers and positions.

Runs under ``python -m unittest`` (no third-party dependencies).
"""
import unittest

from regex_automata.errors import LexError
from regex_automata.lexer import lex
from regex_automata.locations import Span
from regex_automata.tokens import (
    T_ANCHOR, T_CHAR, T_DOT, T_EOF, T_LPAREN, T_PIPE,
    T_QUANT, T_RPAREN,
)


def kinds(source):
    return [t.kind for t in lex(source)]


class LexerTests(unittest.TestCase):
    def test_simple_token_stream(self):
        self.assertEqual(kinds("a.b|(c)*+?"), [
            T_CHAR, T_DOT, T_CHAR, T_PIPE, T_LPAREN, T_CHAR, T_RPAREN,
            T_QUANT, T_QUANT, T_QUANT, T_EOF,
        ])

    def test_positions_are_code_point_offsets(self):
        toks = lex("(ab)")
        self.assertEqual(toks[0].span.start, 0)
        self.assertEqual((toks[1].span.start, toks[1].span.end), (1, 2))
        self.assertEqual(toks[-2].span.start, 3)
        toks = lex("é+")
        self.assertEqual(toks[0].span, Span(0, 1))
        self.assertEqual(toks[1].span.start, 1)

    def test_quantifier_forms(self):
        src = "a*b+c?d{2}e{2,}f{2,4}g{0,0}"
        q = [t for t in lex(src) if t.kind == T_QUANT]
        self.assertEqual([(t.minimum, t.maximum) for t in q], [
            (0, None), (1, None), (0, 1), (2, 2), (2, None), (2, 4), (0, 0),
        ])

    def test_bare_brace_is_literal(self):
        toks = lex("a{,3}")
        self.assertEqual(toks[0].kind, T_CHAR)
        self.assertEqual(toks[0].cp, ord("a"))
        self.assertEqual(toks[1].kind, T_CHAR)
        self.assertEqual(toks[1].cp, ord("{"))

    def test_class_basic_and_negation(self):
        pred = lex("[abc]")[0].predicate
        self.assertTrue(pred.matches(ord("a")) and pred.matches(ord("c")))
        self.assertFalse(pred.matches(ord("d")))
        neg = lex("[^abc]")[0].predicate
        self.assertFalse(neg.matches(ord("a")))
        self.assertTrue(neg.matches(ord("d")))
        self.assertTrue(neg.matches(ord("\n")))

    def test_class_ranges_and_shorthands(self):
        pred = lex("[a-zA-Z0-9_]")[0].predicate
        self.assertTrue(pred.matches(ord("z")) and pred.matches(ord("9")))
        self.assertFalse(pred.matches(ord("-")))
        ws = lex(r"[\s\d]")[0].predicate
        self.assertTrue(ws.matches(ord(" ")) and ws.matches(ord("7")))
        self.assertFalse(ws.matches(ord("a")))

    def test_class_leading_brackets_and_dash(self):
        pred = lex("[]a]")[0].predicate
        self.assertTrue(pred.matches(ord("]")) and pred.matches(ord("a")))
        self.assertFalse(lex("[^]a]")[0].predicate.matches(ord("]")))
        pred2 = lex("[a-]")[0].predicate
        self.assertTrue(pred2.matches(ord("-")) and pred2.matches(ord("a")))

    def test_escapes(self):
        self.assertEqual(lex(r"\n")[0].cp, 0x0A)
        self.assertEqual(lex(r"\t")[0].cp, 0x09)
        self.assertEqual(lex(r"\x41")[0].cp, ord("A"))
        self.assertEqual(lex(r"é")[0].cp, ord("é"))
        self.assertEqual(lex(r"\.")[0].kind, T_CHAR)
        self.assertEqual(lex(r"\\")[0].cp, ord("\\"))

    def test_anchor_tokens(self):
        anchors = [t.anchor for t in lex(r"^\b\B$") if t.kind == T_ANCHOR]
        self.assertEqual(anchors, ["^", "b", "B", "$"])

    def test_quantifier_advances_scanner(self):
        # regression: {n} must consume its characters (no infinite loop)
        self.assertEqual(kinds("x{2}"), [T_CHAR, T_QUANT, T_EOF])

    def test_lex_errors(self):
        bad = ["\\", "[", "*]", "a{3,2}", "a{1001}", r"\q", r"[z-a]",
               r"\x4", r"\u12", r"[\b]"]
        for b in bad:
            with self.subTest(bad=b):
                with self.assertRaises(LexError):
                    lex(b)


if __name__ == "__main__":
    unittest.main()
