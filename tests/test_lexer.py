"""Tests for the lexer (positions are part of the contract)."""

import unittest

from miniml.lexer import LexError, lex


class TestLexer(unittest.TestCase):
    def test_keywords_and_identifiers(self):
        toks = lex("let rec x = true in")
        kinds = [t.kind for t in toks]
        self.assertEqual(kinds, ["let", "rec", "IDENT", "=", "true", "in", "EOF"])

    def test_operators_prefer_longest(self):
        kinds = [t.kind for t in lex("< <= <> >= := -> && ||")]
        self.assertEqual(kinds, ["<", "<=", "<>", ">=", ":=", "->", "&&", "||", "EOF"])

    def test_integer_value(self):
        t = lex("42")[0]
        self.assertEqual(t.kind, "INT")
        self.assertEqual(t.text, "42")

    def test_positions_line_col(self):
        toks = lex("let x =\n  1 ;;")
        # the integer literal is on line 2, column 3
        i = next(i for i, t in enumerate(toks) if t.kind == "INT")
        self.assertEqual((toks[i].span.start.line, toks[i].span.start.col), (2, 3))
        self.assertEqual((toks[i].span.end.line, toks[i].span.end.col), (2, 4))

    def test_nested_comments(self):
        toks = lex("(* outer (* inner *) still *) 5")
        self.assertEqual([t.kind for t in toks], ["INT", "EOF"])

    def test_unterminated_comment_reports_position(self):
        with self.assertRaises(LexError) as cm:
            lex("(* never ends")
        self.assertEqual(cm.exception.pos.line, 1)
        self.assertEqual(cm.exception.pos.col, 1)

    def test_number_followed_by_letters_is_error(self):
        with self.assertRaises(LexError) as cm:
            lex("123abc")
        self.assertTrue("number" in cm.exception.message)

    def test_unknown_character(self):
        with self.assertRaises(LexError):
            lex("@")


if __name__ == "__main__":
    unittest.main()
