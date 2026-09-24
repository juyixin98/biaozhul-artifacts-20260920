"""SemVer 版本比较与范围语法测试。"""
import pytest

from app.semver import RangeSyntaxError, parse_range, parse_version


class TestVersionOrdering:
    def test_release_ordering(self):
        assert parse_version("1.0.0") < parse_version("1.0.1")
        assert parse_version("1.9.9") < parse_version("1.10.0")
        assert parse_version("1.10.0") < parse_version("2.0.0")

    def test_prerelease_ordering_semver_org_chain(self):
        # semver.org 规范中的经典排序链
        chain = [
            "1.0.0-alpha",
            "1.0.0-alpha.1",
            "1.0.0-alpha.beta",
            "1.0.0-beta",
            "1.0.0-beta.2",
            "1.0.0-beta.11",
            "1.0.0-rc.1",
            "1.0.0",
        ]
        versions = [parse_version(v) for v in chain]
        for earlier, later in zip(versions, versions[1:]):
            assert earlier < later, f"{earlier} 应小于 {later}"

    def test_numeric_identifier_less_than_alphanumeric(self):
        assert parse_version("1.0.0-1") < parse_version("1.0.0-alpha")

    def test_build_metadata_ignored(self):
        assert parse_version("1.0.0+build.1") == parse_version("1.0.0+build.2")
        assert parse_version("1.0.0-rc.1+xyz") == parse_version("1.0.0-rc.1")

    def test_invalid_version_rejected(self):
        for bad in ["latest", "1.0", "1.0.x", "abc", "1.0.0.0"]:
            with pytest.raises(ValueError):
                parse_version(bad)


class TestRangeMatching:
    @pytest.mark.parametrize(
        "range_text,version,expected",
        [
            # 精确与比较符
            ("1.2.3", "1.2.3", True),
            ("1.2.3", "1.2.4", False),
            (">=1.0.0 <1.4.2", "1.4.1", True),
            (">=1.0.0 <1.4.2", "1.4.2", False),   # 上界排除
            (">=1.0.0 <1.4.2", "1.0.0", True),    # 下界包含
            (">=1.0.0 <1.4.2", "0.9.9", False),
            ("<=2.0.0", "2.0.0", True),
            (">1.5.0", "1.5.0", False),
            # 部分版本与通配符
            ("1.2", "1.2.9", True),
            ("1.2", "1.3.0", False),
            ("1.2.x", "1.2.0", True),
            ("1.x", "1.99.0", True),
            ("*", "0.0.1", True),
            ("<1.2", "1.1.9", True),
            ("<=1.2", "1.2.9", True),   # <=1.2 表示 <1.3.0
            (">1.2", "1.3.0", True),    # >1.2 表示 >=1.3.0
            # caret / tilde
            ("^1.2.3", "1.9.0", True),
            ("^1.2.3", "2.0.0", False),
            ("^0.2.3", "0.2.9", True),
            ("^0.2.3", "0.3.0", False),
            ("^0.0.3", "0.0.3", True),
            ("^0.0.3", "0.0.4", False),
            ("~1.2.3", "1.2.9", True),
            ("~1.2.3", "1.3.0", False),
            ("~1.2", "1.2.0", True),
            ("~1.2", "1.3.0", False),
            # 并集
            (">=1.5.0 <1.5.9 || >=2.0.0-alpha <2.0.0", "1.5.8", True),
            (">=1.5.0 <1.5.9 || >=2.0.0-alpha <2.0.0", "1.5.9", False),
            (">=1.5.0 <1.5.9 || >=2.0.0-alpha <2.0.0", "2.0.0-rc.1", True),
            (">=1.5.0 <1.5.9 || >=2.0.0-alpha <2.0.0", "2.0.0", False),
            # 预发布门控：集合内无同三元组预发布比较符时不命中
            ("<2.0.0", "2.0.0-alpha", False),
            (">=1.0.0 <2.0.0", "1.5.0-beta", False),
            (">=2.0.0-alpha <2.0.0", "2.0.0-alpha", True),
            (">=2.0.0-alpha <2.0.0", "2.0.0-rc.1", True),
            (">=2.0.0-alpha <2.0.0", "2.0.0", False),
            ("^1.2.3-beta", "1.2.3-beta.1", True),
            ("^1.2.3-beta", "1.2.4-beta", False),  # 三元组不同，门控不通过
            # 逗号分隔等价于空格
            (">=1.0.0, <2.0.0", "1.5.0", True),
        ],
    )
    def test_matches(self, range_text, version, expected):
        assert parse_range(range_text).matches(parse_version(version)) is expected

    def test_hyphen_range_rejected(self):
        with pytest.raises(RangeSyntaxError):
            parse_range("1.2.3 - 2.0.0")

    def test_empty_range_rejected(self):
        with pytest.raises(RangeSyntaxError):
            parse_range("   ")

    def test_garbage_rejected(self):
        with pytest.raises(RangeSyntaxError):
            parse_range(">=abc")
