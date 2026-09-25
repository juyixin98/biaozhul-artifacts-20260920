"""词法与语法分析测试：重点验证源码位置与错误处理。"""

import unittest

from renfa.errors import RegexSyntaxError
from renfa.lexer import (
    T_ANCHOR_END,
    T_ANCHOR_START,
    T_DOT,
    T_LITERAL,
    T_LPAREN,
    T_PIPE,
    T_PLUS,
    T_QUESTION,
    T_REPEAT,
    T_RPAREN,
    T_STAR,
    tokenize,
)
from renfa.parser import parse
from renfa import ast_nodes as ast


def parse_one(pattern):
    src, toks = tokenize(pattern)
    return parse(src, toks)


class LexerTests(unittest.TestCase):
    def test_basic_tokens(self):
        _, toks = tokenize("a|b.c*")
        kinds = [t.kind for t in toks]
        self.assertEqual(
            kinds,
            [T_LITERAL, T_PIPE, T_LITERAL, T_DOT, T_LITERAL, T_STAR, "EOF"],
        )
        self.assertEqual(toks[0].value, ord("a"))
        self.assertEqual(toks[0].span.start, 0)
        self.assertEqual(toks[0].span.end, 1)

    def test_quantifier_forms(self):
        cases = {"a{2}": (2, 2), "b{3,}": (3, None), "c{1,4}": (1, 4)}
        for text, val in cases.items():
            with self.subTest(text=text):
                _, toks = tokenize(text)
                self.assertEqual(toks[1].kind, T_REPEAT)
                self.assertEqual(toks[1].value, val)
                self.assertEqual(toks[1].raw, text[1:])

    def test_escapes(self):
        _, toks = tokenize(r"\n\t\.\\\(\)")
        self.assertEqual([t.value for t in toks[:-1]], [10, 9, ord("."),
                                                        ord("\\"), ord("("),
                                                        ord(")")])

    def test_unicode_escape_forms(self):
        _, toks = tokenize(r"中中\u{1F600}")
        self.assertEqual(toks[0].value, 0x4E2D)
        self.assertEqual(toks[1].value, 0x4E2D)
        self.assertEqual(toks[2].value, 0x1F600)

    def test_unicode_escape_rejects_surrogate(self):
        with self.assertRaises(RegexSyntaxError) as cm:
            tokenize(r"\uD800")
        self.assertIn("代理码点", cm.exception.message)
        self.assertEqual(cm.exception.span.start, 0)

    def test_unsupported_class_escapes(self):
        for esc in (r"\d", r"\w", r"\s", r"\b"):
            with self.subTest(esc=esc), self.assertRaises(RegexSyntaxError):
                tokenize(esc)

    def test_character_class_not_supported(self):
        with self.assertRaises(RegexSyntaxError):
            tokenize("[abc]")

    def test_backreference_not_supported(self):
        for pat in (r"(a)\1", r"\2"):
            with self.subTest(pat=pat):
                with self.assertRaises(RegexSyntaxError):
                    tokenize(pat)
        # \0 是空字符转义，仍合法；\1 才是反向引用
        _, toks = tokenize(r"\0")
        self.assertEqual(toks[0].value, 0)

    def test_bare_brace_is_error(self):
        with self.assertRaises(RegexSyntaxError):
            tokenize("a{2")
        with self.assertRaises(RegexSyntaxError):
            tokenize("{}")
        with self.assertRaises(RegexSyntaxError):
            tokenize("a}b")

    def test_trailing_backslash(self):
        with self.assertRaises(RegexSyntaxError) as cm:
            tokenize("ab\\")
        self.assertEqual(cm.exception.span.start, 2)

    def test_position_multiline(self):
        src, toks = tokenize("ab\ncd")
        # token 序列: a(0) b(1) \n(2) c(3) d(4) EOF；c 位于第 2 行第 1 列
        self.assertEqual(toks[3].value, ord("c"))
        self.assertEqual(toks[3].span.start_line, 2)
        self.assertEqual(toks[3].span.start_col, 1)

    def test_emoji_offset_is_one_codepoint(self):
        # '😀' 是单个码点（虽然 UTF-16 下是代理对）
        _, toks = tokenize("😀a")
        self.assertEqual(len(toks), 3)  # emoji, a, EOF
        self.assertEqual(toks[1].span.start, 1)


class ParserTests(unittest.TestCase):
    def test_concat_tree(self):
        tree = parse_one("ab")
        self.assertIsInstance(tree, ast.Concat)
        self.assertEqual(len(tree.children), 2)

    def test_alt_tree_and_spans(self):
        tree = parse_one("ab|c")
        self.assertIsInstance(tree, ast.Alt)
        self.assertEqual(tree.span.start, 0)
        self.assertEqual(tree.span.end, 4)

    def test_group(self):
        tree = parse_one("(ab)+")
        rep = tree
        self.assertIsInstance(rep, ast.Repeat)
        self.assertEqual((rep.mn, rep.mx), (1, None))
        self.assertIsInstance(rep.child, ast.Group)

    def test_quantifier_normalization(self):
        self.assertEqual(_q("a*"), (0, None))
        self.assertEqual(_q("a+"), (1, None))
        self.assertEqual(_q("a?"), (0, 1))
        self.assertEqual(_q("a{2,5}"), (2, 5))

    def test_empty_alternatives(self):
        tree = parse_one("a|")
        self.assertIsInstance(tree.right, ast.Empty)
        tree = parse_one("|a")
        self.assertIsInstance(tree.left, ast.Empty)
        tree = parse_one("()")
        self.assertIsInstance(tree.child, ast.Empty)

    def test_quantifier_without_atom(self):
        for bad in ("*", "a|*", "(*)", "+a", "?"):
            with self.subTest(bad=bad), self.assertRaises(RegexSyntaxError):
                parse_one(bad)

    def test_stacked_quantifiers_rejected(self):
        with self.assertRaises(RegexSyntaxError) as cm:
            parse_one("a**")
        self.assertIn("叠加", cm.exception.message)
        # 但显式括号允许
        tree = parse_one("(a*)+")
        self.assertIsInstance(tree, ast.Repeat)

    def test_unbalanced_parens(self):
        with self.assertRaises(RegexSyntaxError):
            parse_one("(a")
        with self.assertRaises(RegexSyntaxError) as cm:
            parse_one("a)")
        self.assertEqual(cm.exception.span.start_col, 2)

    def test_repeat_min_gt_max(self):
        with self.assertRaises(RegexSyntaxError):
            parse_one("a{5,2}")

    def test_error_message_shows_location(self):
        try:
            parse_one("abc|*")
        except RegexSyntaxError as e:
            text = str(e)
            self.assertIn("第 1 行", text)
            self.assertIn("^", text)
        else:
            self.fail("应当抛出语法错误")


def _q(pattern):
    tree = parse_one(pattern)
    return tree.mn, tree.mx


if __name__ == "__main__":
    unittest.main()
