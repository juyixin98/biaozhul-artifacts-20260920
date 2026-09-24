"""Tests for the self-contained semver implementation."""

from __future__ import annotations

import pytest

from app.semver import (
    InvalidVersionError,
    UnsupportedRangeError,
    Version,
    parse_range,
    satisfies,
)


@pytest.mark.parametrize(
    "version,spec,expected",
    [
        ("1.2.3", "1.2.3", True),
        ("1.2.3", "1.2.4", False),
        ("1.2.3", "^1.2.0", True),
        ("1.3.0", "^1.2.0", True),
        ("2.0.0", "^1.2.0", False),
        ("0.2.9", "^0.2.3", True),
        ("0.3.0", "^0.2.3", False),
        ("0.0.3", "^0.0.3", True),
        ("0.0.4", "^0.0.3", False),
        ("1.2.9", "~1.2.1", True),
        ("1.3.0", "~1.2.1", False),
        ("2.0.0", "~1", False),
        ("1.9.0", "~1", True),
        ("1.2.3", ">=1.0.0 <2.0.0", True),
        ("2.0.0", ">=1.0.0 <2.0.0", False),
        ("1.2.5", "1.2.0 - 1.2.9", True),
        ("1.3.0", "1.2.0 - 1.2.9", False),
        ("2.1.0", "1.x || 2.x", True),
        ("3.0.0", "1.x || 2.x", False),
        ("1.2.3", "*", True),
        ("9.9.9", "", True),
        ("1.8.0", "1", True),
        ("2.0.0", "1", False),
        ("1.2.3", "1.2", True),
        ("1.3.0", "1.2", False),
        ("1.2.3-beta.2", "^1.2.3-beta.2", True),
        ("1.2.4-beta.2", "^1.2.3-beta.2", False),
        ("1.2.3-beta.2", "^1.2.3", False),
        ("1.2.3", ">=1.2.3-beta.1", True),
        ("1.2.3", "v1.2.3", True),
        ("0.0.0", "0.0.x", True),
    ],
)
def test_satisfies_npm_cases(version, spec, expected):
    assert satisfies(version, spec) is expected


@pytest.mark.parametrize("bad_version", ["1.2", "v", "01.2.3", "1.2.3.4", "abc"])
def test_invalid_versions_rejected(bad_version):
    with pytest.raises(InvalidVersionError):
        Version.parse(bad_version)


@pytest.mark.parametrize(
    "spec",
    [
        "file:../local.tgz",
        "link:../pkg",
        "git+ssh://git@host/repo.git",
        "git+https://host/repo.git",
        "github:owner/repo",
        "owner/repo#abcdef",
        "npm:left-pad@1.0.0",
        "http://insecure.example/x.tgz",
        "https://registry.example/x.tgz",
        "portal:../other",
        "workspace:^",
        "latest",
        "^1.x ||",
    ],
)
def test_unsupported_specs_raise(spec):
    with pytest.raises(UnsupportedRangeError):
        parse_range(spec)


def test_prerelease_ordering():
    assert Version.parse("1.0.0-alpha") < Version.parse("1.0.0")
    assert Version.parse("1.0.0-alpha.1") < Version.parse("1.0.0-alpha.beta")
    assert Version.parse("1.0.0-alpha.beta") < Version.parse("1.0.0-beta")
    assert Version.parse("1.0.0-beta") < Version.parse("1.0.0-beta.2")
    assert Version.parse("1.0.0-beta.2") < Version.parse("1.0.0-rc.1")
    assert Version.parse("1.0.0-rc.1") < Version.parse("1.0.0")
