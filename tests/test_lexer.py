import unittest

from lattlang.errors import LangError
from lattlang.lexer import tokenize


class TestLexer(unittest.TestCase):
    def test_tokens_and_spans(self):
        toks = tokenize("x := 12;")
        kinds = [t.kind for t in toks]
        self.assertEqual(kinds, ["IDENT", ":=", "INT", ";", "EOF"])
        self.assertEqual(toks[2].value, 12)
        self.assertEqual((toks[0].span.start_line, toks[0].span.start_col), (1, 1))
        self.assertEqual(toks[2].span.start_col, 6)

    def test_multiline_positions(self):
        toks = tokenize("a := 1\nb := 2")
        idents = [t for t in toks if t.kind == "IDENT"]
        self.assertEqual([(t.text, t.span.start_line) for t in idents],
                         [("a", 1), ("b", 2)])

    def test_operators(self):
        toks = tokenize("<= >= == != && ||")
        self.assertEqual([t.kind for t in toks[:-1]],
                         ["<=", ">=", "==", "!=", "&&", "||"])

    def test_comment(self):
        toks = tokenize("x := 1 # trailing comment\ny := 2")
        idents = [t.text for t in toks if t.kind == "IDENT"]
        self.assertEqual(idents, ["x", "y"])

    def test_keywords(self):
        toks = tokenize("if else while print true false")
        self.assertEqual([t.kind for t in toks[:-1]],
                         ["if", "else", "while", "print", "true", "false"])

    def test_illegal_character(self):
        with self.assertRaises(LangError) as cm:
            tokenize("x @= 1")
        self.assertEqual(cm.exception.stage, "lex")
        self.assertEqual(cm.exception.span.start_col, 3)


if __name__ == "__main__":
    unittest.main()
