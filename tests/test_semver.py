"""Unit tests for the hand-rolled semver engine."""
from __future__ import annotations

import pytest

from app.lockgate.semver import UnsupportedSpecError, parse_version, satisfies


@pytest.mark.parametrize("text,expected", [
    ("1.2.3", (1, 2, 3)),
    ("v1.2.3", (1, 2, 3)),
    ("1.2.3-rc.1", (1, 2, 3)),
    ("1.2.3+build.7", (1, 2, 3)),
    ("0.0.0", (0, 0, 0)),
])
def test_parse_version_ok(text, expected):
    v = parse_version(text)
    assert (v.major, v.minor, v.patch) == expected


@pytest.mark.parametrize("text", ["1.2", "1", "1.2.3.4", "v", "1.02.3", "1.2.3-01", "v1"])
def test_parse_version_rejects(text):
    with pytest.raises(UnsupportedSpecError):
        parse_version(text)


@pytest.mark.parametrize("candidate,spec,want", [
    # caret
    ("1.2.3", "^1.0.0", True),
    ("2.0.0", "^1.0.0", False),
    ("0.2.5", "^0.2.0", True),
    ("0.3.0", "^0.2.0", False),
    ("0.0.3", "^0.0.3", True),
    ("0.0.4", "^0.0.3", False),
    # tilde
    ("1.2.9", "~1.2.0", True),
    ("1.3.0", "~1.2.0", False),
    ("1.5.0", "~1", True),
    ("2.0.0", "~1", False),
    # x ranges
    ("1.5.9", "1.x", True),
    ("2.0.0", "1.x", False),
    ("1.2.5", "1.2.x", True),
    ("1.3.0", "1.2.x", False),
    # comparators, hyphen, OR
    ("1.5.0", ">=1.0.0 <2.0.0", True),
    ("2.0.0", ">=1.0.0 <2.0.0", False),
    ("1.5.0", "1.0.0 - 1.5.0", True),
    ("1.5.1", "1.0.0 - 1.5.0", False),
    ("2.1.0", "^1.0.0 || ^2.0.0", True),
    ("3.0.0", "^1.0.0 || ^2.0.0", False),
    ("1.2.3", "*", True),
    ("1.2.3", "", True),
    ("1.2.3", "1.2.3", True),
    ("1.2.4", "1.2.3", False),
    ("1.2.3", "=1.2.3", True),
    # joined comparators without spaces
    ("2.0.0", ">=1.0.0<3.0.0", True),
    # prerelease handling (node-semver rule)
    ("1.0.0-alpha", "^1.0.0", False),
    ("1.0.0-alpha", "^1.0.0-alpha", True),
    ("1.0.0-beta", ">=1.0.0-alpha <1.0.0", True),
    ("1.0.0", ">=1.0.0-alpha", True),
    # prerelease precedence
    ("1.0.0-alpha.2", ">1.0.0-alpha.1", True),
    ("1.0.0-alpha", "<1.0.0-alpha.1", True),
    ("1.0.0-alpha", "<1.0.0-beta", True),
])
def test_satisfies(candidate, spec, want):
    assert satisfies(candidate, spec) is want


@pytest.mark.parametrize("spec", [
    "github:foo/bar",
    "git+https://example.com/x.git",
    "file:../local",
    "link:./x",
    "workspace:^",
    "npm:renamed@^1.0.0",
    "latest",
    "foo/bar",
    "https://example.com/x.tgz",
    ">?",
])
def test_unsupported_specs_are_refused(spec):
    with pytest.raises(UnsupportedSpecError):
        satisfies("1.0.0", spec)


def test_prerelease_build_metadata_ignored_for_precedence():
    assert satisfies("1.0.0+exp.sha.5114f85", "1.0.0")
