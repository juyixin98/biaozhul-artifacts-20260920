"""规范化 JSON 测试：确定性、重复键、非法类型、Unicode 归一化。"""

import unittest

from sig_manifest.canonjson import (
    canonical_bytes,
    canonicalize,
    encode,
    normalize,
    parse,
)
from sig_manifest.errors import CanonicalJSONError


class TestCanonicalRoundTrip(unittest.TestCase):
    def test_field_reorder_byte_identical(self) -> None:
        """字段重排（对象键）必须产生完全相同的规范化字节。

        注意：数组是有序的，元素顺序不同则字节不同——这是预期行为，
        本用例保持文件数组顺序不变，只重排对象键。
        """
        a = (
            b'{"name":"demo","files":[{"path":"a","size":1},'
            b'{"path":"b","size":2}],"nonce":"x"}'
        )
        b = (
            b'{"nonce":"x","files":[{"size":1,"path":"a"},'
            b'{"size":2,"path":"b"}],"name":"demo"}'
        )
        data_a, bytes_a = canonicalize(a)
        data_b, bytes_b = canonicalize(b)
        self.assertEqual(bytes_a, bytes_b)
        self.assertEqual(data_a, data_b)

    def test_array_order_is_significant(self) -> None:
        """数组元素换序必须产生不同字节（顺序是载荷语义的一部分）。"""
        a = b'{"files":[{"path":"a"},{"path":"b"}]}'
        b = b'{"files":[{"path":"b"},{"path":"a"}]}'
        _, bytes_a = canonicalize(a)
        _, bytes_b = canonicalize(b)
        self.assertNotEqual(bytes_a, bytes_b)

    def test_compact_no_whitespace(self) -> None:
        data, raw = canonicalize(b'{\n  "a": 1,\n  "b": [true, null, "x"]\n}')
        self.assertEqual(raw, b'{"a":1,"b":[true,null,"x"]}')

    def test_key_sort_by_codepoint(self) -> None:
        _, raw = canonicalize('{"é":1,"z":2,"a":3,"中":4}')
        # ASCII 字母先，然后是 U+00E9，再 U+4E2D。
        self.assertEqual(raw, '{"a":3,"z":2,"é":1,"中":4}'.encode("utf-8"))

    def test_unicode_not_ascii_escaped(self) -> None:
        _, raw = canonicalize('{"greeting":"你好"}')
        self.assertEqual(raw, '{"greeting":"你好"}'.encode("utf-8"))

    def test_required_escapes(self) -> None:
        _, raw = canonicalize(r'{"a":"\"\\\b\f\n\r\t\u0001"}')
        self.assertIn(rb'\"', raw)
        self.assertIn(rb'\\', raw)
        self.assertIn(rb'\b', raw)
        self.assertIn(rb'\u0001', raw)

    def test_slash_not_escaped(self) -> None:
        _, raw = canonicalize('{"p":"a/b"}')
        self.assertEqual(raw, b'{"p":"a/b"}')

    def test_integer_forms(self) -> None:
        _, raw = canonicalize('{"big":9007199254740993,"neg":-5,"zero":0}')
        self.assertEqual(raw, b'{"big":9007199254740993,"neg":-5,"zero":0}')


class TestRejectBadJSON(unittest.TestCase):
    def test_duplicate_key_rejected(self) -> None:
        with self.assertRaises(CanonicalJSONError) as ctx:
            parse('{"a":1,"a":2}')
        self.assertIn("重复键", str(ctx.exception))

    def test_duplicate_nested_key_rejected(self) -> None:
        with self.assertRaises(CanonicalJSONError):
            parse('{"outer":{"k":1,"k":2}}')

    def test_duplicate_key_after_json_loads_would_merge(self) -> None:
        # 标准 json.loads 会静默保留后者；我们必须显式拒绝。
        with self.assertRaises(CanonicalJSONError):
            parse(b'{"nonce":"a","nonce":"b"}')

    def test_float_rejected(self) -> None:
        with self.assertRaises(CanonicalJSONError):
            normalize(parse('{"x":1.0}'))

    def test_nan_infinity_rejected(self) -> None:
        for bad in ("NaN", "Infinity", "-Infinity"):
            with self.subTest(bad=bad), self.assertRaises(CanonicalJSONError):
                parse('{"x":' + bad + "}")

    def test_bom_rejected(self) -> None:
        with self.assertRaises(CanonicalJSONError):
            parse(b'\xef\xbb\xbf{"a":1}')

    def test_trailing_content_rejected(self) -> None:
        with self.assertRaises(CanonicalJSONError):
            parse('{"a":1}junk')

    def test_non_object_top_level_rejected(self) -> None:
        with self.assertRaises(CanonicalJSONError):
            canonicalize('[1,2,3]')
        with self.assertRaises(CanonicalJSONError):
            canonicalize('"hello"')

    def test_duplicate_key_with_space_variants(self) -> None:
        with self.assertRaises(CanonicalJSONError):
            parse('{ "a" : 1, "a" : 2 }')

    def test_nfc_collision_rejected(self) -> None:
        # U+00EA (ê) 与 e + U+0302（组合扬抑符）NFC 后相同。
        composed = "ê"  # 单个码点
        decomposed = "e" + "̂"  # 两个码点，NFC 后等于 composed
        self.assertNotEqual(composed, decomposed)
        text = '{"' + composed + '":1,"' + decomposed + '":2}'
        data = parse(text)  # 解析本身允许（两键在 NFC 前不同）
        with self.assertRaises(CanonicalJSONError):
            normalize(data)

    def test_string_nfc_normalized(self) -> None:
        decomposed = "e" + "̂"  # e + combining circumflex
        out = normalize({"v": decomposed})
        self.assertEqual(out["v"], "ê")
        # 归一化结果可编码。
        encode(out)


class TestDeterminismAcrossInputs(unittest.TestCase):
    def test_pretty_vs_compact(self) -> None:
        pretty = b'{\n  "z": [1,\n     2],\n  "a": {\n   "y": true,\n   "x": null\n}\n}'
        compact = b'{"a":{"x":null,"y":true},"z":[1,2]}'
        _, raw_pretty = canonicalize(pretty)
        _, raw_compact = canonicalize(compact)
        self.assertEqual(raw_pretty, raw_compact)

    def test_bool_not_confused_with_int(self) -> None:
        data, raw = canonicalize('{"t":true,"f":false,"i":1}')
        self.assertIs(data["t"], True)
        self.assertEqual(raw, b'{"f":false,"i":1,"t":true}')


if __name__ == "__main__":
    unittest.main()
