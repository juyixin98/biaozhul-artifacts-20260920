"""SemVer subset tests: ordering, pre-release precedence, range endpoints."""
import pytest

from app.versions.errors import VersionError
from app.versions.semver import parse_semver, semver_satisfies


def _cmp(a, b):
    from app.versions.semver import _compare
    return _compare(parse_semver(a), parse_semver(b))


class TestSemverOrder:
    def test_numeric_not_string(self):
        # string comparison would say "10.0.0" < "2.0.0"
        assert _cmp("10.0.0", "2.0.0") > 0
        assert _cmp("1.9.0", "1.10.0") < 0

    def test_loose_versions(self):
        assert semver_satisfies("1", "1.x")
        assert semver_satisfies("1.2", "1.2.x")

    def test_prerelease_ordering(self):
        assert _cmp("1.0.0-alpha", "1.0.0") < 0
        assert _cmp("1.0.0-alpha", "1.0.0-alpha.1") < 0
        assert _cmp("1.0.0-alpha.1", "1.0.0-alpha.beta") < 0
        assert _cmp("1.0.0-alpha.beta", "1.0.0-beta") < 0
        assert _cmp("1.0.0-beta", "1.0.0-beta.2") < 0
        assert _cmp("1.0.0-beta.2", "1.0.0-beta.11") < 0
        assert _cmp("1.0.0-beta.11", "1.0.0-rc.1") < 0
        assert _cmp("1.0.0-rc.1", "1.0.0") < 0
        # numeric prerelease id compares numerically, not lexically
        assert _cmp("2.0.0-rc.1", "2.0.0-rc.2") < 0
        assert _cmp("2.0.0-rc.10", "2.0.0-rc.2") > 0

    def test_invalid(self):
        with pytest.raises(VersionError):
            parse_semver("banana")
        with pytest.raises(VersionError):
            parse_semver("1.2.3.4")


class TestSemverRanges:
    @pytest.mark.parametrize("ver,expr,expected", [
        ("1.4.1", ">=1.2.0 <1.4.2", True),
        ("1.4.2", ">=1.2.0 <1.4.2", False),   # exclusive upper endpoint
        ("1.2.0", ">=1.2.0 <1.4.2", True),    # inclusive lower endpoint
        ("1.1.9", ">=1.2.0 <1.4.2", False),
        ("1.2", "1.2.x", True),
        ("1.3.9", "1.2.x", False),
        ("2.1.0", "1 || 2.x", True),
        ("3.0.0", "1 || 2.x", False),
        ("1.2.3", "~1.2.3", True),
        ("1.2.9", "~1.2.3", True),
        ("1.3.0", "~1.2.3", False),
        ("1.2.0", "~1.2", True),
        ("1.3.0", "~1.2", False),
        ("0.2.9", "^0.2.3", True),
        ("0.3.0", "^0.2.3", False),
        ("0.0.3", "^0.0.3", True),
        ("0.0.4", "^0.0.3", False),
        ("1.4.2", "^1.2.3", True),
        ("2.0.0", "^1.2.3", False),
        ("1.2.3", "1.2.3 - 2.3.4", True),
        ("2.3.4", "1.2.3 - 2.3.4", True),
        ("2.3.5", "1.2.3 - 2.3.4", False),
        ("1.2", "1.2 - 2.3.4", True),
        ("2.4.0", "1.2 - 2.3.4", False),
    ])
    def test_endpoints(self, ver, expr, expected):
        assert semver_satisfies(ver, expr) is expected

    def test_prerelease_tag_rule(self):
        # prerelease version not matching any comparator tuple -> excluded
        assert semver_satisfies("2.0.0-rc.1", ">=1.0.0 <2.0.0-rc.2") is True
        assert semver_satisfies("2.0.0-rc.2", ">=1.0.0 <2.0.0-rc.2") is False
        assert semver_satisfies("2.0.0-rc.1", ">=1.0.0") is False
        assert semver_satisfies("2.0.0", ">=1.0.0") is True
