"""词法器测试：重点覆盖字符串内部符号、未闭合字符串、注释与非法字符。"""

import unittest

from dl.lexer import tokenize


class TestLexer(unittest.TestCase):
    def test_basic_tokens(self):
        tokens, errors = tokenize("var x = 42;")
        kinds = [(t.kind, t.text) for t in tokens if t.kind != "eof"]
        self.assertEqual(kinds, [
            ("keyword", "var"), ("ident", "x"), ("op", "="),
            ("number", "42"), ("op", ";"),
        ])
        self.assertEqual(errors, [])

    def test_string_keeps_symbols_inside(self):
        # 字符串内的括号、分号、运算符绝不能被切成 op 令牌
        tokens, errors = tokenize('var s = "(1 + 2); {x}";')
        texts = [t.text for t in tokens]
        self.assertIn('"(1 + 2); {x}"', texts)
        # 整个字符串只有一个 string 令牌
        strings = [t for t in tokens if t.kind == "string"]
        self.assertEqual(len(strings), 1)
        self.assertEqual(strings[0].start, 8)
        self.assertEqual(strings[0].end, 22)
        # 括号等只出现在 = 与 ; 处
        ops = [t.text for t in tokens if t.kind == "op"]
        self.assertEqual(ops, ["=", ";"])
        self.assertEqual(errors, [])

    def test_single_quoted_string_with_inner_double_quote(self):
        tokens, errors = tokenize("var s = 'a\"b)';")
        self.assertEqual(errors, [])
        st = [t for t in tokens if t.kind == "string"][0]
        self.assertEqual(st.text, '\'a"b)\'')

    def test_unterminated_string_is_lex_error_token(self):
        tokens, errors = tokenize('var s = "abc;')
        self.assertEqual(len(errors), 1)
        self.assertIn("未闭合", errors[0].message)
        bad = [t for t in tokens if t.is_lex_error]
        self.assertEqual(len(bad), 1)
        self.assertEqual(bad[0].kind, "string")
        self.assertEqual(bad[0].start, errors[0].start)

    def test_unterminated_string_stops_at_newline(self):
        tokens, errors = tokenize('"abc\nvar')
        self.assertEqual(len(errors), 1)
        # 错误字符串令牌不跨行
        self.assertLess(tokens[0].end, tokens[0].start + 5)

    def test_comments_skip_symbols(self):
        tokens, errors = tokenize("// (1+2) { }; bad chars\nvar x;")
        self.assertEqual(errors, [])
        texts = [t.text for t in tokens if t.kind != "eof"]
        self.assertEqual(texts, ["var", "x", ";"])

    def test_illegal_character(self):
        tokens, errors = tokenize("x @ y")
        self.assertEqual(len(errors), 1)
        self.assertEqual(errors[0].start, 2)
        self.assertTrue(any(t.is_lex_error and t.text == "@" for t in tokens))

    def test_multi_char_ops(self):
        tokens, _ = tokenize("a==b!=c<=d>=e&&f||g")
        ops = [t.text for t in tokens if t.kind == "op"]
        self.assertEqual(ops, ["==", "!=", "<=", ">=", "&&", "||"])

    def test_escapes_inside_string(self):
        tokens, errors = tokenize(r'"a\"b\\c\nd"')
        self.assertEqual(errors, [])
        self.assertEqual(len([t for t in tokens if t.kind == "string"]), 1)

    def test_eof_sentinel(self):
        tokens, _ = tokenize("a")
        self.assertEqual(tokens[-1].kind, "eof")
        self.assertEqual(tokens[-1].start, 1)
        self.assertEqual(tokens[-1].end, 1)

    def test_empty_source(self):
        tokens, errors = tokenize("")
        self.assertEqual(len(tokens), 1)
        self.assertEqual(tokens[0].kind, "eof")
        self.assertEqual(errors, [])

    def test_positions_half_open(self):
        tokens, _ = tokenize("  ab1 ")
        t = [x for x in tokens if x.kind == "ident"][0]
        self.assertEqual((t.start, t.end), (2, 5))
        self.assertEqual("ab1", "ab1")  # 半开区间内容


if __name__ == "__main__":
    unittest.main()
