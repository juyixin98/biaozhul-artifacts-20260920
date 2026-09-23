"""Functional tests for matching semantics (standard-library unittest)."""
import unittest

from regex_automata import ast, compile_pattern
from regex_automata.errors import ParseError


def m(pattern, text, mode="search"):
    return getattr(compile_pattern(pattern), mode)(text)


def t(match):
    return None if match is None else (match.start, match.end, match.matched)


SEARCH_CASES = [
    ("abc", "abc", (0, 3, "abc")),
    ("abc", "xabcx", (1, 4, "abc")),
    ("abc", "abd", None),
    ("", "abc", (0, 0, "")),
    ("", "", (0, 0, "")),
    ("a|ab", "ab", (0, 2, "ab")),
    ("ab|a", "ab", (0, 2, "ab")),
    ("(ab|a)(b|c)", "ac", (0, 2, "ac")),
    ("cat|dog|bird", "I have a dog", (9, 12, "dog")),
]

FULLMATCH_CASES = [
    ("abc", "abc", True), ("abc", "xabc", False),
    ("a*", "", True), ("a*", "aaa", True), ("a*", "aab", False),
    ("(a|b)*", "ababc", False), ("(a|b)+", "abab", True),
    ("[0-9]{3}", "123", True), ("[0-9]{3}", "12", False),
]

PREFIX_CASES = [
    ("abc", "abcdef", (0, 3, "abc")),
    ("abc", "xabc", None),
    ("a*", "aaab", (0, 3, "aaa")),
    ("", "xyz", (0, 0, "")),
]

REPEAT_CASES = [
    ("a{3}", "aaaa", (0, 3, "aaa")),
    ("a{2,4}", "aaaaa", (0, 4, "aaaa")),
    ("a{2,}", "aaaaaa", (0, 6, "aaaaaa")),
    ("a{0,2}", "aaa", (0, 2, "aa")),
    ("a{0}", "aaa", (0, 0, "")),
    ("(ab){2}", "ababx", (0, 4, "abab")),
    ("colou?r", "colour", (0, 6, "colour")),
]

ANCHOR_CASES = [
    ("^abc", "abc", (0, 3, "abc")),
    ("^abc", "xabc", None),
    ("abc$", "xabc", (1, 4, "abc")),
    ("abc$", "abcd", None),
    ("^abc$", "abc", (0, 3, "abc")),
    ("^$", "", (0, 0, "")),
    ("^$", "x", None),
    (r"\bfoo\b", "foo", (0, 3, "foo")),
    (r"\bfoo\b", "a foo b", (2, 5, "foo")),
    (r"\bfoo\b", "foobar", None),
    (r"\bfoo\b", "xfoo", None),
    (r"foo\b", "foo!", (0, 3, "foo")),
    (r"\Bfoo\B", "sfoos", (1, 4, "foo")),
    (r"\B.", "abc", (1, 2, "b")),
]

CLASS_CASES = [
    ("[abc]+", "xcabx", (1, 4, "cab")),
    ("[^abc]+", "cabdef", (3, 6, "def")),
    ("[a-cA-C0-2]+", "B2xyz", (0, 2, "B2")),
    (r"\d+", "abc12345def", (3, 8, "12345")),
    (r"\D+", "12abc3", (2, 5, "abc")),
    (r"\w+", "x = héllo", (0, 1, "x")),       # leftmost run is "x"
    (r"\w+", "= héllo", (2, 7, "héllo")),     # non-ASCII word run
    (r"\s+", "a \n\tb", (1, 4, " \n\t")),
    (r"[\d\s]+", "1 2 3x", (0, 5, "1 2 3")),
    ("[^0-9]", "9a", (1, 2, "a")),
    ("é+", "été éé", (0, 1, "é")),
    ("😀+", "x😀😀y", (1, 3, "😀😀")),
    (r"\w+", "日本語", (0, 3, "日本語")),
]


class MatcherTests(unittest.TestCase):
    def test_search_cases(self):
        for pat, text, expected in SEARCH_CASES:
            with self.subTest(pat=pat, text=text):
                self.assertEqual(t(m(pat, text)), expected)

    def test_leftmost_then_longest(self):
        self.assertEqual(t(m("a|bb", "bba")), (0, 2, "bb"))
        self.assertEqual(t(m("a|ab", "ab")), (0, 2, "ab"))

    def test_fullmatch_cases(self):
        for pat, text, ok in FULLMATCH_CASES:
            with self.subTest(pat=pat, text=text):
                self.assertEqual(m(pat, text, "fullmatch") is not None, ok)

    def test_prefix_cases(self):
        for pat, text, expected in PREFIX_CASES:
            with self.subTest(pat=pat, text=text):
                self.assertEqual(t(m(pat, text, "prefix")), expected)

    def test_repetition(self):
        for pat, text, expected in REPEAT_CASES:
            with self.subTest(pat=pat):
                self.assertEqual(t(m(pat, text)), expected)

    def test_nested_quantifiers_terminate(self):
        self.assertEqual(t(m("(a*)*", "aaa")), (0, 3, "aaa"))
        self.assertEqual(t(m("(a*)*b", "aaab")), (0, 4, "aaab"))
        self.assertEqual(t(m("(a?){5}", "aaaaa")), (0, 5, "aaaaa"))
        self.assertEqual(t(m("(x|)*", "xxx")), (0, 3, "xxx"))
        self.assertEqual(t(m("(|a)*", "b")), (0, 0, ""))

    def test_anchors(self):
        for pat, text, expected in ANCHOR_CASES:
            with self.subTest(pat=pat, text=text):
                self.assertEqual(t(m(pat, text)), expected)

    def test_classes_and_unicode(self):
        for pat, text, expected in CLASS_CASES:
            with self.subTest(pat=pat, text=text):
                self.assertEqual(t(m(pat, text)), expected)

    def test_dot_excludes_newline_only(self):
        self.assertEqual(t(m(".", "a")), (0, 1, "a"))
        self.assertIsNone(m(".", "\n"))
        self.assertIsNone(m("a.b", "a\nb"))
        self.assertEqual(t(m("a.b", "a b")), (0, 3, "a b"))
        self.assertEqual(t(m(".", "\r")), (0, 1, "\r"))

    def test_unicode_word_boundary(self):
        self.assertEqual(t(m(r"\bé\b", "é")), (0, 1, "é"))
        self.assertEqual(t(m("héllo\\b", "héllo")), (0, 5, "héllo"))
        self.assertIsNone(m(r"x\b", "xé"))

    def test_ast_spans_preserved(self):
        rep = compile_pattern("(ab){3}").tree
        self.assertIsInstance(rep, ast.Repeat)
        self.assertEqual((rep.span.start, rep.span.end), (0, 7))
        self.assertIsInstance(rep.child, ast.Group)
        self.assertEqual((rep.child.span.start, rep.child.span.end), (0, 4))

    def test_error_has_position(self):
        with self.assertRaises(ParseError) as ctx:
            compile_pattern("  (ab")
        self.assertEqual(ctx.exception.span.start, 2)

    def test_multiple_bare_quantifier_rejected_but_grouped_allowed(self):
        with self.assertRaises(ParseError):
            compile_pattern("a**")
        # grouping makes it legal and equivalent to the child language
        self.assertIsNotNone(m("(a*)*", "aaa"))


if __name__ == "__main__":
    unittest.main()
