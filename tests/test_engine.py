"""匹配引擎功能测试：连接、选择、括号、有限重复、锚点、Unicode 语义。"""

import unittest

from renfa import Regex


class BasicMatchTests(unittest.TestCase):
    def test_literal_concat(self):
        self.assertTrue(Regex("abc").fullmatch("abc"))
        self.assertFalse(Regex("abc").fullmatch("abd"))
        self.assertFalse(Regex("abc").fullmatch("ab"))

    def test_alternation(self):
        r = Regex("a|b|c")
        self.assertTrue(r.fullmatch("a"))
        self.assertTrue(r.fullmatch("c"))
        self.assertFalse(r.fullmatch("d"))
        self.assertTrue(Regex("ab|cd").fullmatch("cd"))

    def test_grouping(self):
        r = Regex("a(b|c)d")
        self.assertTrue(r.fullmatch("abd"))
        self.assertTrue(r.fullmatch("acd"))
        self.assertFalse(r.fullmatch("ad"))
        self.assertFalse(r.fullmatch("aed"))

    def test_repeat_exact(self):
        r = Regex("a{3}")
        self.assertTrue(r.fullmatch("aaa"))
        self.assertFalse(r.fullmatch("aa"))
        self.assertFalse(r.fullmatch("aaaa"))

    def test_repeat_bounded_range(self):
        r = Regex("a{2,4}")
        for n in (2, 3, 4):
            self.assertTrue(r.fullmatch("a" * n), n)
        for n in (0, 1, 5):
            self.assertFalse(r.fullmatch("a" * n), n)

    def test_repeat_lower_only(self):
        r = Regex("a{2,}")
        self.assertTrue(r.fullmatch("aa"))
        self.assertTrue(r.fullmatch("a" * 10))
        self.assertFalse(r.fullmatch("a"))

    def test_star_plus_question(self):
        self.assertTrue(Regex("a*").fullmatch(""))
        self.assertTrue(Regex("a*").fullmatch("aaa"))
        self.assertFalse(Regex("a+").fullmatch(""))
        self.assertTrue(Regex("a?").fullmatch(""))
        self.assertTrue(Regex("a?").fullmatch("a"))
        self.assertFalse(Regex("a?").fullmatch("aa"))

    def test_compound(self):
        r = Regex("(ab|c)(d|e)*f{1,2}")
        self.assertTrue(r.fullmatch("abf"))
        self.assertTrue(r.fullmatch("cddeff"))
        self.assertTrue(r.fullmatch("cff"))
        self.assertFalse(r.fullmatch("ff"))
        self.assertFalse(r.fullmatch("abfff"))

    def test_dot_semantics(self):
        r = Regex("a.c")
        self.assertTrue(r.fullmatch("abc"))
        self.assertTrue(r.fullmatch("a中c"))
        self.assertFalse(r.fullmatch("a\nc"))  # . 不匹配 \n
        self.assertFalse(r.fullmatch("ac"))

    def test_anchor_full_string_only(self):
        r = Regex("^abc$")
        self.assertTrue(r.fullmatch("abc"))
        self.assertFalse(r.fullmatch("xabc"))
        # 无 MULTILINE：^ 不匹配内部行首
        self.assertIsNone(r.search("xx\nabc"))
        # $ 不匹配末尾换行之前
        self.assertIsNone(Regex("abc$").search("abc\n"))

    def test_anchor_in_alternation(self):
        r = Regex("^a|b$")
        self.assertIsNotNone(r.search("a"))
        self.assertIsNotNone(r.search("xxb"))
        self.assertIsNone(r.search("c"))

    def test_empty_pattern(self):
        r = Regex("")
        m = r.search("abc")
        self.assertEqual((m.start, m.end), (0, 0))
        self.assertTrue(r.fullmatch(""))


class UnicodeTests(unittest.TestCase):
    def test_codepoint_is_unit(self):
        # emoji 是单个码点；{2} 指两个码点，不是两个 UTF-16 单元
        r = Regex("😀{2}")
        self.assertTrue(r.fullmatch("😀😀"))
        self.assertFalse(r.fullmatch("😀"))

    def test_dotted_han_and_offsets(self):
        r = Regex("中.")
        m = r.search("x中文")
        self.assertEqual((m.start, m.end), (1, 3))
        self.assertEqual(m.text, "中文")

    def test_dot_does_not_span_combining(self):
        # 不做规范化：e + 组合重音 U+0301 是两个码点，'.' 只吃一个
        decomposed = "é"
        self.assertEqual(len(decomposed), 2)
        self.assertTrue(Regex("..").fullmatch(decomposed))
        self.assertFalse(Regex(".").fullmatch(decomposed))

    def test_no_case_folding(self):
        self.assertFalse(Regex("a").fullmatch("A"))


class SearchSemanticsTests(unittest.TestCase):
    def test_leftmost_longest(self):
        # 左起始优先
        m = Regex("a|ab").search("ab")
        self.assertEqual((m.start, m.end), (0, 2))  # 同起始取最长
        m = Regex("ab|b").search("ab")
        self.assertEqual((m.start, m.end), (0, 2))
        m = Regex("b|ab").search("ab")
        self.assertEqual((m.start, m.end), (0, 2))

    def test_search_finds_later(self):
        m = Regex("abc").search("xxabcxx")
        self.assertEqual((m.start, m.end), (2, 5))

    def test_match_anchored_at_zero(self):
        r = Regex("abc")
        self.assertIsNotNone(r.match("abcxx"))
        self.assertIsNone(r.match("xxabc"))

    def test_findall_non_overlapping(self):
        r = Regex("a{1,2}")
        ms = [(m.start, m.end, m.text) for m in r.findall("aaa")]
        self.assertEqual(ms, [(0, 2, "aa"), (2, 3, "a")])

    def test_findall_zero_width_advances(self):
        r = Regex("a*")
        ms = [(m.start, m.end) for m in r.findall("ab")]
        self.assertEqual(ms, [(0, 1), (1, 1), (2, 2)])

    def test_findall_emoji_offsets(self):
        r = Regex("😀+")
        ms = [(m.start, m.end, m.text) for m in r.findall("x😀😀y")]
        self.assertEqual(ms, [(1, 3, "😀😀")])


if __name__ == "__main__":
    unittest.main()
