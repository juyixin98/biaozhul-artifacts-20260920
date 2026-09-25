"""Lexer unit tests: token kinds, positions, comments, error locations."""

import unittest

from resflow.lexer import Lexer, LexError, Location


def lex(src):
    return [(t.kind, t.value) for t in Lexer(src).tokenize()]


class TestLexer(unittest.TestCase):

    def test_keywords_and_identifiers(self):
        toks = lex("fn acquire release use return throw if else while")
        kinds = [k for k, _ in toks if k != "EOF"]
        self.assertEqual(kinds, ["KEYWORD"] * 9)

    def test_punctuation_canonical_kinds(self):
        toks = lex("( ) { } ; ,")
        kinds = [k for k, _ in toks if k != "EOF"]
        self.assertEqual(kinds, ["LPAREN", "RPAREN", "LBRACE",
                                 "RBRACE", "SEMI", "COMMA"])

    def test_operators_two_char_greedy(self):
        toks = lex("== != <= >= && ||")
        vals = [v for k, v in toks if k != "EOF"]
        self.assertEqual(vals, ["==", "!=", "<=", ">=", "&&", "||"])

    def test_equality_not_assignment(self):
        toks = lex("= ==")
        vals = [v for _, v in toks if _ != "EOF"]
        self.assertEqual(vals, ["=", "=="])

    def test_literals(self):
        toks = lex('42 7 true false "hi"')
        non_eof = toks[:-1]
        self.assertEqual(non_eof[0], ("INT", 42))
        self.assertEqual(non_eof[2], ("BOOL", "true"))
        self.assertEqual(non_eof[4], ("STRING", "hi"))

    def test_string_escapes(self):
        toks = lex(r'"a\nb\tc\\d\""')
        self.assertEqual(toks[0][1], 'a\nb\tc\\d"')

    def test_line_comments(self):
        toks = lex("fn // nothing here\n")
        self.assertEqual(toks[0], ("KEYWORD", "fn"))
        self.assertEqual(toks[1][0], "EOF")

    def test_block_comment_nested(self):
        src = "a /* outer /* inner */ still comment */ b"
        toks = lex(src)
        names = [v for k, v in toks if k == "IDENT"]
        self.assertEqual(names, ["a", "b"])

    def test_unterminated_block_comment(self):
        with self.assertRaises(LexError) as cm:
            lex("/* never ends")
        self.assertEqual(cm.exception.location.line, 1)

    def test_unterminated_string(self):
        with self.assertRaises(LexError):
            lex('"oops')

    def test_unknown_character(self):
        with self.assertRaises(LexError) as cm:
            lex("@")
        self.assertEqual(cm.exception.location.offset, 0)

    def test_locations_are_one_based_line_column(self):
        src = "fn x() {\n  acquire(r);\n}"
        toks = Lexer(src).tokenize()
        acquire = next(t for t in toks if t.value == "acquire")
        self.assertEqual(acquire.loc.line, 2)
        self.assertEqual(acquire.loc.column, 3)
        # Half-open span covers exactly the keyword.
        self.assertEqual(src[acquire.loc.offset:acquire.loc.end_offset],
                         "acquire")

    def test_location_after_multiple_lines(self):
        src = "// c\n\nlet x = 1;"
        toks = Lexer(src).tokenize()
        let = toks[0]
        self.assertEqual(let.loc.line, 3)
        self.assertEqual(let.loc.column, 1)


if __name__ == "__main__":
    unittest.main()
