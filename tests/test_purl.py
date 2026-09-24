"""purl 解析与规范化测试，含错误输入。"""
from __future__ import annotations

import pytest

from app.purl import PurlError, normalize_ecosystem, parse_purl


def test_basic_purl_fields():
    p = parse_purl("pkg:npm/left-pad@6.10.3")
    assert p.type == "npm"
    assert p.name == "left-pad"
    assert p.namespace is None
    assert p.version == "6.10.3"
    assert p.ecosystem == "npm"


def test_namespace_and_qualifiers_preserved():
    p = parse_purl("pkg:maven/com.example/http-core@3.1.0?type=jar")
    assert p.namespace == "com.example"
    assert p.name == "http-core"
    assert p.version == "3.1.0"
    assert p.qualifiers == {"type": "jar"}


def test_variants_are_distinct_canonical_keys():
    base = parse_purl("pkg:npm/left-pad@6.10.3")
    lin = parse_purl("pkg:npm/left-pad@6.10.3?os=linux")
    mac = parse_purl("pkg:npm/left-pad@6.10.3?os=darwin")
    keys = {base.canonical(), lin.canonical(), mac.canonical()}
    assert len(keys) == 3


def test_same_purl_canonical_equal_regardless_of_case():
    a = parse_purl("pkg:NPM/Left-Pad@6.10.3")
    b = parse_purl("pkg:npm/left-pad@6.10.3")
    assert a.canonical() == b.canonical()


def test_percent_decoding():
    p = parse_purl("pkg:npm/some%20scope/lib@1.0.0")
    assert p.namespace == "some scope"
    assert p.name == "lib"


def test_ecosystem_aliases():
    assert normalize_ecosystem("PyPI") == "pypi"
    assert normalize_ecosystem("rubygems") == "gem"


@pytest.mark.parametrize("bad", [
    "", "   ", "not-a-purl", "http://example.com/x",
    "pkg://npm/left-pad@1.0.0",  # URL 形式不在子集内
    "pkg:", "pkg:/left-pad@1.0.0",
    "pkg:???/x@1.0.0",
    "pkg:/x@1.0.0",
    "pkg:npm/@1.0.0",
])
def test_invalid_purls_raise(bad):
    with pytest.raises(PurlError):
        parse_purl(bad)


def test_missing_version_is_allowed_but_none():
    p = parse_purl("pkg:npm/versionless")
    assert p.version is None


def test_unknown_ecosystem_parses_but_is_not_supported():
    p = parse_purl("pkg:weird-eco/widget@1.2.3")
    assert p.ecosystem == "weird-eco"
    from app.purl import SUPPORTED_ECOSYSTEMS
    assert p.ecosystem not in SUPPORTED_ECOSYSTEMS
