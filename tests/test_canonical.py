"""规范化 JSON 规则测试。"""

import pytest

from sam.canonical import (
    canonical_bytes,
    canonical_text,
    canonicalize,
    parse_strict,
)
from sam.errors import CanonicalJSONError


def test_basic_object_sorted_keys():
    # 字段重排: 输入键序不同, 规范字节一致
    a = canonical_bytes({"b": 1, "a": 2})
    b = canonical_bytes({"a": 2, "b": 1})
    assert a == b == b'{"a":2,"b":1}'


def test_nested_and_array_order_preserved():
    doc = {"z": [3, 1, 2], "a": {"y": True, "x": None}}
    assert canonical_text(doc) == '{"a":{"x":null,"y":true},"z":[3,1,2]}'


def test_unicode_emitted_raw_utf8():
    out = canonical_bytes({"名": "值 ✓"})
    assert out == '{"名":"值 ✓"}'.encode("utf-8")


def test_control_chars_escaped():
    assert canonical_text({"a": "\n\t\x01"}) == r'{"a":"\n\t\u0001"}'
    # 值为双引号 + 反斜杠两个字符 -> \" 和 \\
    assert canonical_text({"q": '"\\'}) == r'{"q":"\"\\"}'


def test_integer_and_float_rules():
    assert canonical_text({"n": 10**30}) == '{"n":' + str(10**30) + "}"
    assert canonical_text({"x": 1.0}) == '{"x":1.0}'
    assert canonical_text({"x": -0.0}) == '{"x":-0.0}'
    # 最短往返 (Python repr)
    assert canonical_text({"x": 0.1}) == '{"x":0.1}'


def test_bool_not_number_and_null():
    assert canonical_text([True, False, None]) == "[true,false,null]"


def test_duplicate_keys_rejected_parse():
    with pytest.raises(CanonicalJSONError, match="重复"):
        parse_strict(b'{"a":1,"a":2}')
    with pytest.raises(CanonicalJSONError):
        parse_strict('{"x":{"k":1,"k":2}}')


def test_non_finite_constants_rejected():
    for bad in ("NaN", "Infinity", "-Infinity"):
        with pytest.raises(CanonicalJSONError):
            parse_strict(bad)
        with pytest.raises(CanonicalJSONError):
            parse_strict(f'{{"v":{bad}}}'.encode())


def test_bom_rejected():
    with pytest.raises(CanonicalJSONError, match="BOM"):
        parse_strict("﻿{}".encode("utf-8"))


def test_trailing_bytes_rejected():
    with pytest.raises(CanonicalJSONError, match="多余字节"):
        parse_strict(b'{}garbage')


def test_scalar_toplevel_rejected():
    with pytest.raises(CanonicalJSONError):
        parse_strict(b"42")
    with pytest.raises(CanonicalJSONError):
        parse_strict(b'"hi"')


def test_unsupported_python_types():
    with pytest.raises(CanonicalJSONError):
        canonical_bytes({"x": {1, 2}})
    with pytest.raises(CanonicalJSONError):
        canonical_bytes({"x": b"abc"})


def test_lone_surrogate_rejected():
    with pytest.raises(CanonicalJSONError):
        canonical_text({"x": "\ud800"})


def test_canonical_roundtrip_stable():
    raw = b'{\n  "b": [2, 1],\n  "a" :  { "y": "v", "x": null }\n}\n'
    once = canonicalize(raw)
    twice = canonicalize(once)
    assert once == twice
    assert once == b'{"a":{"x":null,"y":"v"},"b":[2,1]}'


def test_key_order_unicode_codepoints():
    # U+00E9 (e-acute) 与 U+0065 (e): 按码位 0x65 < 0xE9
    out = canonical_text({"é": 1, "e": 2})
    assert out == '{"e":2,"é":1}'


def test_empty_containers():
    assert canonical_text({}) == "{}"
    assert canonical_text([]) == "[]"
