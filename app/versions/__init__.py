"""Version parsing / range evaluation, dispatched per ecosystem.

Three *real* version models are implemented (no string ordering anywhere):

* ``npm``    — a documented SemVer subset (loose parser + npm range semantics)
* ``pypi``   — a documented PEP 440 subset
* ``maven``  — Maven ``ComparableVersion`` token ordering + version ranges
"""
from __future__ import annotations

from .errors import VersionError
from .maven import maven_satisfies, parse_maven
from .pep440 import parse_pep440, pep440_satisfies
from .semver import parse_semver, semver_satisfies

ECOSYSTEMS = ("npm", "pypi", "maven")

_PARSERS = {
    "npm": parse_semver,
    "pypi": parse_pep440,
    "maven": parse_maven,
}


def parse_version(ecosystem: str, text: str):
    """Parse a concrete component version. Raises VersionError on failure."""
    try:
        parser = _PARSERS[ecosystem]
    except KeyError as exc:  # pragma: no cover - guarded by callers
        raise VersionError(f"unsupported ecosystem: {ecosystem!r}") from exc
    return parser(text)


def satisfies(ecosystem: str, version: str, range_expr: str) -> bool:
    """Return True iff concrete ``version`` satisfies ``range_expr``.

    Raises VersionError if either the version or the range expression
    cannot be parsed for the given ecosystem.
    """
    if ecosystem == "npm":
        return semver_satisfies(version, range_expr)
    if ecosystem == "pypi":
        return pep440_satisfies(version, range_expr)
    if ecosystem == "maven":
        return maven_satisfies(version, range_expr)
    raise VersionError(f"unsupported ecosystem: {ecosystem!r}")
