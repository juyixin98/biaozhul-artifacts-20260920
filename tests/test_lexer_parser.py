"""Unit tests for lexer and parser, including source-location preservation."""

import unittest

from slang.errors import LexError, ParseError
from slang.lexer import tokenize
from slang.parser import parse


class LexerTests(unittest.TestCase):
    def test_tokens_have_locations(self):
        toks = tokenize("let x = 42;")
        kinds = [t.kind for t in toks if t.kind != "EOF"]
        self.assertEqual(kinds, ["KEYWORD:let", "ID", "=", "INT", ";"])
        idtok = toks[1]
        self.assertEqual(idtok.value, "x")
        self.assertEqual((idtok.loc.line, idtok.loc.col), (1, 5))
        self.assertEqual((idtok.loc.offset, idtok.loc.end_offset), (4, 5))

    def test_line_tracking(self):
        toks = tokenize("\n\n  abc")
        idtok = [t for t in toks if t.kind == "ID"][0]
        self.assertEqual((idtok.loc.line, idtok.loc.col), (3, 3))

    def test_strings_and_escapes(self):
        toks = tokenize(r'"a\nb" + "x"')
        self.assertEqual(toks[0].value, "a\nb")
        self.assertEqual(toks[0].kind, "STR")

    def test_comments(self):
        toks = tokenize("1 // line comment\n /* block */ 2")
        self.assertEqual([t.value for t in toks if t.kind == "INT"], [1, 2])

    def test_nested_block_comments(self):
        toks = tokenize("/* a /* b */ c */ 9")
        self.assertEqual([t.value for t in toks if t.kind == "INT"], [9])

    def test_bad_char(self):
        with self.assertRaises(LexError) as cm:
            tokenize("@")
        self.assertEqual(cm.exception.loc.line, 1)

    def test_unterminated_string(self):
        with self.assertRaises(LexError):
            tokenize('"abc')

    def test_unterminated_comment(self):
        with self.assertRaises(LexError):
            tokenize("/* nope")


class ParserTests(unittest.TestCase):
    def test_precedence(self):
        tree = parse("print(1 + 2 * 3);")
        call = tree.stmts[0].expr
        binop = call.args[0]
        self.assertEqual(binop.op, "+")
        self.assertEqual(binop.right.op, "*")

    def test_left_assoc(self):
        tree = parse("print(10 - 4 - 3);")
        b = tree.stmts[0].expr.args[0]
        self.assertEqual(b.left.op, "-")

    def test_unary_and_parens(self):
        tree = parse("print(-(1 + 2));")
        u = tree.stmts[0].expr.args[0]
        self.assertEqual(u.op, "-")

    def test_dangling_else(self):
        tree = parse("if (a) { if (b) {1;} else {2;} }")
        outer = tree.stmts[0]
        inner = outer.then.stmts[0]
        self.assertIsNotNone(inner.otherwise)
        # else-branch block contains the literal statement `2;`
        self.assertEqual(inner.otherwise.stmts[0].expr.value, 2)

    def test_named_and_anon_functions(self):
        tree = parse("let g = fn f(a, b) { return a; }; let h = fn() {};")
        self.assertEqual(tree.stmts[0].init.name, "f")
        self.assertEqual(len(tree.stmts[0].init.params), 2)
        self.assertIsNone(tree.stmts[1].init.name)

    def test_expected_token_error_location(self):
        with self.assertRaises(ParseError) as cm:
            parse("let = 1;")
        self.assertIsNotNone(cm.exception.loc)

    def test_program_loc_span(self):
        tree = parse("1;\n2;\n")
        self.assertEqual(tree.loc.end_line, 3)

    def test_chained_calls(self):
        tree = parse("f(1)(2);")
        outer = tree.stmts[0].expr          # f(1)(2)
        inner = outer.callee                # f(1)
        self.assertEqual(inner.args[0].value, 1)
        self.assertEqual(outer.args[0].value, 2)


if __name__ == "__main__":
    unittest.main()
