"""PEP 440 subset tests."""
import pytest

from app.versions.errors import VersionError
from app.versions.pep440 import parse_pep440, pep440_satisfies


class TestPEP440Ordering:
    def test_numeric_release_order(self):
        assert pep440_satisfies("1.10.0", ">1.9.0")
        assert not pep440_satisfies("1.9.0", ">1.10.0")

    def test_prerelease_chain(self):
        order = ["1.0.dev1", "1.0a1", "1.0a2.dev1", "1.0a2", "1.0b1",
                 "1.0rc1", "1.0", "1.0.post1"]
        for lo, hi in zip(order, order[1:]):
            assert pep440_satisfies(hi, f">{lo}"), f"{hi} should be > {lo}"

    def test_normalization(self):
        assert parse_pep440("1.0.0").release == (1, 0, 0)

    def test_invalid(self):
        with pytest.raises(VersionError):
            parse_pep440("not-a-version")


class TestPEP440Specifiers:
    @pytest.mark.parametrize("ver,expr,expected", [
        ("1.1.0", ">=1.0.0,<1.2.3", True),
        ("1.2.3", ">=1.0.0,<1.2.3", False),       # exclusive endpoint
        ("1.0.0", ">=1.0.0,<1.2.3", True),
        ("2.0b1", ">=2.0b1,<2.0", True),
        ("2.0b2", ">=2.0b1,<2.0", True),
        ("2.0", ">=2.0b1,<2.0", False),
        ("1.9.9", ">=2.0b1,<2.0", False),
        ("3.1.5", "~=3.1.0", True),
        ("3.2.0", "~=3.1.0", False),
        ("3.1", "~=3.1.0", True),
        ("4.0.0", "~=3.1.0", False),
        ("1.2.3", "==1.2.*", True),
        ("1.3.0", "==1.2.*", False),
        ("1.2.9", "!=1.2.*", False),
        ("1.3.0", "!=1.2.*", True),
        ("1.4.0", ">=1.0,<2.0", True),
        ("1.4", "==1.4.0", True),                 # zero-padding on equality
    ])
    def test_specifiers(self, ver, expr, expected):
        assert pep440_satisfies(ver, expr) is expected

    def test_prerelease_exclusion(self):
        # without an explicit pre-release boundary, prereleases are excluded
        assert pep440_satisfies("2.0b1", ">=1.0,<2.0") is False
        assert pep440_satisfies("2.0b1", ">=2.0b1,<2.0") is True

    def test_bad_specifier(self):
        with pytest.raises(VersionError):
            pep440_satisfies("1.0", "~=1")
