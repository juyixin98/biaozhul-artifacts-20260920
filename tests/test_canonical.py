import bootstrap  # noqa: F401

import unittest

from anti_replay.canonical import (
    CanonicalizationError,
    canonical_path,
    canonical_query,
    canonical_request_target,
    split_request_target,
)


class CanonicalPathTests(unittest.TestCase):
    def test_root_and_basic(self):
        self.assertEqual(canonical_path("/"), "/")
        self.assertEqual(canonical_path("/api/data"), "/api/data")

    def test_dot_segments(self):
        self.assertEqual(canonical_path("/./api/data"), "/api/data")
        self.assertEqual(canonical_path("/api/../api/data"), "/api/data")
        self.assertEqual(canonical_path("/a/b/c/../d/./e"), "/a/b/d/e")

    def test_empty_segments_collapsed(self):
        self.assertEqual(canonical_path("//api//data"), "/api/data")
        self.assertEqual(canonical_path("/api/data/"), "/api/data")

    def test_encoded_letter_normalizes_to_equivalent_path(self):
        # %64 == 'd'：编码歧义必须归一
        self.assertEqual(canonical_path("/api/%64ata"), "/api/data")
        self.assertEqual(canonical_path("/api/%64%61ta"), "/api/data")
        # 大写十六进制也接受，输出统一大写 %HH 仅用于非 unreserved
        self.assertEqual(canonical_path("/%61pi/data"), "/api/data")

    def test_encoded_dotdot_is_still_dotdot(self):
        # 不能靠 %2e 编码绕过点段归一
        self.assertEqual(canonical_path("/api/%2e%2e/api/data"), "/api/data")

    def test_dotdot_escaping_root_rejected(self):
        for bad in ("/../etc/passwd", "/%2e%2e/etc/passwd", "/a/../../etc",
                    "/api/..%2f..%2fetc"):
            with self.subTest(bad=bad):
                with self.assertRaises(CanonicalizationError):
                    canonical_path(bad)

    def test_encoded_separator_rejected(self):
        for bad in ("/api%2fdata", "/api%2Fdata", "/a%2fb/c", "/%2f"):
            with self.subTest(bad=bad):
                with self.assertRaises(CanonicalizationError):
                    canonical_path(bad)

    def test_double_encoded_separator_does_not_collapse(self):
        # %252f 解码一次后是字面 "%2f"，绝不能被当成路径分隔符
        self.assertEqual(canonical_path("/api/%252fdata"), "/api/%252fdata")
        self.assertNotEqual(canonical_path("/api/%252fdata"), "/api/data")

    def test_control_characters_rejected(self):
        for bad in ("/api/%00", "/api/%0a", "/api/%7F", "/api/%1f"):
            with self.subTest(bad=bad):
                with self.assertRaises(CanonicalizationError):
                    canonical_path(bad)

    def test_malformed_percent_encoding_rejected(self):
        for bad in ("/api/%", "/api/%2", "/api/%zz", "/api/%2g/data"):
            with self.subTest(bad=bad):
                with self.assertRaises(CanonicalizationError):
                    canonical_path(bad)

    def test_raw_non_ascii_rejected(self):
        with self.assertRaises(CanonicalizationError):
            canonical_path("/api/数据")

    def test_utf8_percent_encoded_non_ascii_preserved(self):
        # “你好” 的 UTF-8：E4 BD A0 E5 A5 BD
        self.assertEqual(
            canonical_path("/api/%E4%BD%A0%E5%A5%BD"),
            "/api/%E4%BD%A0%E5%A5%BD",
        )

    def test_space_encodes_to_percent20(self):
        self.assertEqual(canonical_path("/api/my%20data"), "/api/my%20data")

    def test_relative_path_rejected(self):
        with self.assertRaises(CanonicalizationError):
            canonical_path("api/data")

    def test_trailing_dot_segment(self):
        self.assertEqual(canonical_path("/api/data/.."), "/api")


class CanonicalQueryTests(unittest.TestCase):
    def test_empty(self):
        self.assertEqual(canonical_query(""), "")

    def test_sorted(self):
        self.assertEqual(canonical_query("b=2&a=1"), "a=1&b=2")
        # 顺序不同但内容相同的查询串规范化结果一致
        self.assertEqual(
            canonical_query("c=3&a=1&b=2"), canonical_query("a=1&b=2&c=3")
        )

    def test_encoded_values_normalized(self):
        self.assertEqual(canonical_query("a=%64"), "a=d")
        self.assertEqual(canonical_query("a=%20"), "a=%20")

    def test_encoded_slash_allowed_inside_value(self):
        # 查询串里 %2F 是值的一部分，不做分隔符解释
        self.assertEqual(canonical_query("a=x%2Fy"), "a=x%2Fy")

    def test_empty_value_allowed(self):
        self.assertEqual(canonical_query("a="), "a=")

    def test_duplicate_key_rejected(self):
        with self.assertRaises(CanonicalizationError):
            canonical_query("a=1&a=2")

    def test_bare_token_rejected(self):
        with self.assertRaises(CanonicalizationError):
            canonical_query("a=1&b")

    def test_empty_key_rejected(self):
        with self.assertRaises(CanonicalizationError):
            canonical_query("=1")

    def test_plus_is_literal(self):
        # 本协议不做 form-urlencoded 解释：'+' 与 '%2B' 等价归一为 %2B
        self.assertEqual(canonical_query("a=b+c"), canonical_query("a=b%2Bc"))
        self.assertEqual(canonical_query("a=b+c"), "a=b%2Bc")

    def test_sort_order_is_byte_order_on_decoded_values(self):
        # k 是 k2 的前缀，故 k 在前；%7A 解码为 'z'，值变为 zb
        self.assertEqual(
            canonical_query("k=%7Ab&k2=a"), "k=zb&k2=a"
        )


class RequestTargetTests(unittest.TestCase):
    def test_split(self):
        self.assertEqual(split_request_target("/a/b?x=1"), ("/a/b", "x=1"))
        self.assertEqual(split_request_target("/a/b"), ("/a/b", ""))
        # 裸 '?' 表示空查询串
        self.assertEqual(split_request_target("/a/b?"), ("/a/b", ""))

    def test_fragment_rejected(self):
        with self.assertRaises(CanonicalizationError):
            canonical_request_target("/a/b?x=1#frag")

    def test_absolute_form_rejected(self):
        with self.assertRaises(CanonicalizationError):
            canonical_request_target("http://evil.example/a")

    def test_full_equivalence_group(self):
        # README 声称等价的一组 raw target 必须规范化成同一对 (path, query)
        targets = [
            "/api/data",
            "/./api/data",
            "//api//data",
            "/api/%64ata",
            "/api/../api/data",
        ]
        normalized = {canonical_request_target(t) for t in targets}
        self.assertEqual(normalized, {("/api/data", "")})

    def test_query_equivalence(self):
        self.assertEqual(
            canonical_request_target("/api/data?b=2&a=1"),
            ("/api/data", "a=1&b=2"),
        )


if __name__ == "__main__":
    unittest.main()
