"""Maven ComparableVersion subset + range interval tests."""
import pytest

from app.versions.errors import VersionError
from app.versions.maven import maven_satisfies, parse_maven_range
from app.versions.maven import _compare


class TestMavenOrder:
    @pytest.mark.parametrize("lo,hi", [
        ("1-alpha-1", "1-alpha-2"),
        ("1-alpha-2", "1-beta-1"),
        ("1-beta-1", "1-milestone-1"),
        ("1-milestone-1", "1-rc-1"),
        ("1-rc-1", "1-snapshot-1"),
        ("1-snapshot-1", "1"),
        ("1", "1-sp-1"),
        ("1.0", "1.1"),
        ("1.0.0", "1.0.1"),
        ("1.9", "1.10"),
        ("1-rc1", "1.0"),
        ("1.0-alpha-1", "1.0"),
    ])
    def test_qualifier_order(self, lo, hi):
        from app.versions.maven import parse_maven
        assert _compare(parse_maven(lo), parse_maven(hi)) < 0
        assert _compare(parse_maven(hi), parse_maven(lo)) > 0
        assert _compare(parse_maven(lo), parse_maven(lo)) == 0

    def test_equal_normalizations(self):
        from app.versions.maven import parse_maven
        assert _compare(parse_maven("1.0"), parse_maven("1.0.0")) == 0
        assert _compare(parse_maven("1"), parse_maven("1.0")) == 0

    def test_invalid(self):
        with pytest.raises(VersionError):
            parse_maven_range("[1.0")


class TestMavenRanges:
    @pytest.mark.parametrize("ver,expr,expected", [
        ("1.0.0", "[1.0.0,1.5.0)", True),
        ("1.4.9", "[1.0.0,1.5.0)", True),
        ("1.5.0", "[1.0.0,1.5.0)", False),     # exclusive upper
        ("0.9.9", "[1.0.0,1.5.0)", False),
        ("1.5.0", "[1.0.0,1.5.0]", True),      # inclusive upper
        ("2.0.0", "[1.0.0,1.5.0)", False),
        ("2.0.0", "[1.0,1.5),[2.0,)", True),  # disjoint OR
        ("1.7.0", "[1.0,1.5),[2.0,)", False),
        ("1.5", "[1.5]", True),                # exact
        ("1.6", "[1.5]", False),
        ("3.0.0", "[2.0,)", True),             # unbounded upper
        ("1.0.0", "[2.0,)", False),
        ("1.0-alpha-3", "[1.0-alpha-1,1.0)", True),
        ("1.0", "[1.0-alpha-1,1.0)", False),
        ("1.0-alpha-1", "[1.0-alpha-1,1.0)", True),
        ("2.0.0", "2.0.0", True),              # soft requirement = exact
        ("2.0.1", "2.0.0", False),
    ])
    def test_intervals(self, ver, expr, expected):
        assert maven_satisfies(ver, expr) is expected

    def test_unbalanced_rejected(self):
        with pytest.raises(VersionError):
            parse_maven_range("[1.0,2.0")
