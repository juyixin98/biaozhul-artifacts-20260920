import unittest

from minilang import lex
from minilang.lexer import decode_string_literal


def token_sigs(tokens):
    return [(t.kind, t.text, t.start, t.end, t.ok) for t in tokens]


class LexerTests(unittest.TestCase):
    def test_simple_tokens(self):
        tokens, diags = lex("let x = 12 + 3.5;")
        kinds = [t.kind for t in tokens]
        self.assertEqual(
            kinds, ["LET", "IDENT", "PUNCT", "NUMBER", "PUNCT",
                    "NUMBER", "PUNCT", "EOF"]
        )
        self.assertEqual(tokens[3].text, "12")
        self.assertEqual(tokens[5].text, "3.5")
        self.assertEqual(diags, [])

    def test_offsets(self):
        tokens, _ = lex("ab\ncd")
        ident1, ident2 = tokens[0], tokens[1]
        self.assertEqual((ident1.start, ident1.end), (0, 2))
        self.assertEqual((ident2.start, ident2.end), (3, 5))

    def test_line_comment(self):
        tokens, diags = lex("1 // + ( let\n 2")
        self.assertEqual([t.kind for t in tokens], ["NUMBER", "NUMBER", "EOF"])
        self.assertEqual(diags, [])

    def test_symbols_inside_string_are_not_tokens(self):
        # The acceptance case: operators, brackets, comment markers and
        # semicolons inside a string must stay part of the STRING token.
        tokens, diags = lex('let s = "a + b // (x;";')
        self.assertEqual(tokens[2].kind, "PUNCT")  # '='
        self.assertEqual(tokens[3].kind, "STRING")
        self.assertEqual(tokens[3].text, '"a + b // (x;"')
        self.assertEqual(tokens[4].kind, "PUNCT")  # ';'
        self.assertEqual(len(tokens), 6)
        self.assertEqual(diags, [])

    def test_string_escapes(self):
        tokens, diags = lex(r'"\n\t\"\\ok"')
        self.assertEqual(tokens[0].kind, "STRING")
        self.assertEqual(
            decode_string_literal(tokens[0].text), '\n\t"\\ok'
        )
        self.assertEqual(diags, [])

    def test_unterminated_string_at_eof(self):
        tokens, diags = lex('"abc')
        self.assertEqual(tokens[0].kind, "STRING")
        self.assertFalse(tokens[0].ok)
        self.assertEqual(len(diags), 1)
        self.assertEqual(diags[0].message, "unterminated string literal")
        self.assertEqual((diags[0].start, diags[0].end), (0, 4))

    def test_unterminated_string_stops_at_newline(self):
        tokens, diags = lex('"abc\n1')
        self.assertFalse(tokens[0].ok)
        self.assertEqual(tokens[0].end, 4)  # excludes newline
        self.assertEqual(tokens[1].kind, "NUMBER")
        self.assertEqual(diags[0].start, 0)

    def test_illegal_character(self):
        tokens, diags = lex("1 @ 2")
        self.assertEqual([t.kind for t in tokens],
                         ["NUMBER", "NUMBER", "EOF"])
        self.assertEqual(len(diags), 1)
        self.assertIn("illegal character", diags[0].message)
        self.assertIn("'@'", diags[0].message)
        self.assertEqual((diags[0].start, diags[0].end), (2, 3))

    def test_keywords(self):
        tokens, _ = lex("let fn")
        self.assertEqual([t.kind for t in tokens[:2]], ["LET", "FN"])


if __name__ == "__main__":
    unittest.main()
