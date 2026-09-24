"""Package URL parsing/canonicalization tests."""
import pytest

from app.purl import PurlError, parse_purl


class TestValidPurl:
    def test_basic_npm(self):
        p = parse_purl("pkg:npm/left-pad-lite@1.4.0")
        assert p.type == "npm"
        assert p.name == "left-pad-lite"
        assert p.version == "1.4.0"
        assert p.canonical() == "pkg:npm/left-pad-lite@1.4.0"

    def test_maven_namespace_and_case(self):
        p = parse_purl("pkg:maven/com.example/Common-Core@1.0")
        assert p.namespace == ("com.example",)
        assert p.name == "Common-Core"  # maven is case sensitive

    def test_pypi_normalizes_case(self):
        p = parse_purl("pkg:pypi/TinyCrypto@1.1.0")
        assert p.name == "tinycrypto"
        assert p.canonical() == "pkg:pypi/tinycrypto@1.1.0"

    def test_namespace_segments(self):
        p = parse_purl("pkg:maven/com/example/common-core@1.0")
        assert p.namespace == ("com", "example")

    def test_qualifiers_participate_in_variant_identity(self):
        a = parse_purl("pkg:npm/caret-lib@0.2.5?arch=x64")
        b = parse_purl("pkg:npm/caret-lib@0.2.5?arch=arm64")
        assert a.variant_key() != b.variant_key()
        same = parse_purl("pkg:npm/caret-lib@0.2.5?arch=arm64")
        assert same.variant_key() == b.variant_key()

    def test_percent_decoding(self):
        p = parse_purl("pkg:npm/%40scope/pkg@1.0")
        assert p.namespace == ("@scope",)
        assert p.name == "pkg"

    def test_subpath(self):
        p = parse_purl("pkg:golang/github.com/foo/bar@v1.2.3#sub/path")
        assert p.subpath == ("sub", "path")


class TestInvalidPurl:
    @pytest.mark.parametrize("text", [
        "",
        "not-a-purl",
        "npm/foo@1.0",
        "pkg:/foo@1.0",
        "pkg:npm/",
        "pkg:npm/@1.0",
        "pkg:npm/%",
    ])
    def test_rejected(self, text):
        with pytest.raises(PurlError):
            parse_purl(text)
