"""A self-contained, real implementation of the subset of the npm ``semver``
specification needed to evaluate Node.js dependency ranges.

Only documented npm range syntax is supported. Anything this module does not
understand raises :class:`UnsupportedRangeError`; callers never fall back to
"accept it" semantics. That explicit rejection is what lets the gate refuse
dependency specs it cannot reason about (git URLs, local paths, aliases,
``file:``/``http:``/``github:`` and so on).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import List, Optional, Tuple, Union

# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class SemverError(ValueError):
    """Base class for all parsing problems."""


class UnsupportedRangeError(SemverError):
    """Raised for dependency specifications the gate deliberately refuses."""


class InvalidVersionError(SemverError):
    """Raised when a claimed semver version is not well formed."""


# ---------------------------------------------------------------------------
# Version model
# ---------------------------------------------------------------------------

_VERSION_RE = re.compile(
    r"^v?(?P<major>0|[1-9]\d*)"
    r"\.(?P<minor>0|[1-9]\d*)"
    r"\.(?P<patch>0|[1-9]\d*)"
    r"(?:-(?P<prerelease>(?:0|[1-9]\d*|\d*[a-zA-Z-][0-9a-zA-Z-]*)"
    r"(?:\.(?:0|[1-9]\d*|\d*[a-zA-Z-][0-9a-zA-Z-]*))*))?"
    r"(?:\+(?P<build>[0-9a-zA-Z-]+(?:\.[0-9a-zA-Z-]+)*))?$"
)

# A partial version as used *inside* a comparator: "1", "1.2", "1.2.3-rc.1".
_PARTIAL_RE = re.compile(
    r"^v?(?P<major>\d+|[xX*])"
    r"(?:\.(?P<minor>\d+|[xX*]))?"
    r"(?:\.(?P<patch>\d+|[xX*]))?"
    r"(?:-(?P<prerelease>[0-9a-zA-Z-]+(?:\.[0-9a-zA-Z-]+)*))?$"
)

_PRERELEASE_IDENT_RE = re.compile(r"^(?:0|[1-9]\d*|\d*[a-zA-Z-][0-9a-zA-Z-]*)$")

# Leading "v" is only accepted on complete/partial semver tokens, never on
# non-semver specifications, and it must be followed by a digit.
_PROTOCOL_PREFIXES = (
    "file:",
    "link:",
    "git:",
    "git+ssh:",
    "git+http:",
    "git+https:",
    "http:",
    "https:",
    "npm:",
    "github:",
    "bitbucket:",
    "gitlab:",
    "workspace:",
    "portal:",
)
_URLISH_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.-]*://")
_GIT_SCOPED_RE = re.compile(
    r"^(?:[^/@\s]+/)?[^/@\s]+#"
)  # github shorthand owner/repo#ref


@dataclass(frozen=True)
class Prerelease:
    parts: Tuple[Union[int, str], ...]


@dataclass(frozen=True)
class Version:
    major: int
    minor: int
    patch: int
    prerelease: Tuple[Union[int, str], ...] = ()
    build: Optional[str] = None

    # -- constructors -----------------------------------------------------

    @classmethod
    def parse(cls, text: str) -> "Version":
        if not isinstance(text, str):
            raise InvalidVersionError(f"version must be a string, got {type(text)!r}")
        match = _VERSION_RE.match(text.strip())
        if not match:
            raise InvalidVersionError(f"{text!r} is not a valid semver version")
        pre = _parse_prerelease(match.group("prerelease"), full=True)
        return cls(
            major=int(match.group("major")),
            minor=int(match.group("minor")),
            patch=int(match.group("patch")),
            prerelease=tuple(pre),
            build=match.group("build"),
        )

    def __str__(self) -> str:  # pragma: no cover - trivial
        base = f"{self.major}.{self.minor}.{self.patch}"
        if self.prerelease:
            base += "-" + ".".join(str(p) for p in self.prerelease)
        if self.build:
            base += "+" + self.build
        return base

    # -- comparison -------------------------------------------------------

    def _cmp_tuple(self) -> tuple:
        return (self.major, self.minor, self.patch)

    def compare(self, other: "Version") -> int:
        if self._cmp_tuple() < other._cmp_tuple():
            return -1
        if self._cmp_tuple() > other._cmp_tuple():
            return 1
        return _compare_prereleases(self.prerelease, other.prerelease)

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, Version):
            return NotImplemented
        return self.compare(other) == 0

    def __lt__(self, other: "Version") -> bool:
        return self.compare(other) < 0

    def __le__(self, other: "Version") -> bool:
        return self.compare(other) <= 0

    def __gt__(self, other: "Version") -> bool:
        return self.compare(other) > 0

    def __ge__(self, other: "Version") -> bool:
        return self.compare(other) >= 0

    def __hash__(self) -> int:
        return hash((self.major, self.minor, self.patch, self.prerelease))


def _parse_prerelease(raw: Optional[str], *, full: bool) -> List[Union[int, str]]:
    if raw is None or raw == "":
        return []
    parts: List[Union[int, str]] = []
    for ident in raw.split("."):
        if full and not _PRERELEASE_IDENT_RE.match(ident):
            raise InvalidVersionError(f"invalid prerelease identifier {ident!r}")
        if ident.isdigit() and not (len(ident) > 1 and ident[0] == "0"):
            parts.append(int(ident))
        else:
            parts.append(ident)
    return parts


def _compare_prereleases(
    a: Tuple[Union[int, str], ...], b: Tuple[Union[int, str], ...]
) -> int:
    # No prerelease has higher precedence than any prerelease.
    if not a and not b:
        return 0
    if not a:
        return 1
    if not b:
        return -1
    for pa, pb in zip(a, b):
        if isinstance(pa, int) and isinstance(pb, int):
            if pa != pb:
                return -1 if pa < pb else 1
        elif isinstance(pa, int):
            return -1  # numeric identifiers always rank lower than strings
        elif isinstance(pb, int):
            return 1
        elif pa != pb:
            return -1 if pa < pb else 1  # lexicographic
    if len(a) == len(b):
        return 0
    return -1 if len(a) < len(b) else 1


# ---------------------------------------------------------------------------
# Range model
# ---------------------------------------------------------------------------


def reject_unsupported_spec(spec: str) -> None:
    """Raise :class:`UnsupportedRangeError` for specifications we refuse.

    This runs *before* any range parsing so that malformed-but-protocol-bearing
    input cannot be accepted through a permissive grammar.
    """
    raw = spec.strip()
    # Empty specs are valid in npm (they mean "*") and are handled by the
    # caller; every other refusal below applies to non-empty specifications.
    if raw == "":
        return
    lowered = raw.lower()
    for prefix in _PROTOCOL_PREFIXES:
        if lowered.startswith(prefix):
            raise UnsupportedRangeError(
                f"{spec!r} uses unsupported protocol/prefix {prefix!r}; the gate "
                "only evaluates pinned/registry semver ranges"
            )
    if "://" in raw or _URLISH_RE.match(raw):
        raise UnsupportedRangeError(f"{spec!r} looks like a URL; URL deps are refused")
    if "github.com/" in lowered or "bitbucket.org/" in lowered or "gitlab.com/" in lowered:
        raise UnsupportedRangeError(f"{spec!r} is a VCS reference; refused")
    if _GIT_SCOPED_RE.match(raw):
        raise UnsupportedRangeError(
            f"{spec!r} is a github-style owner/repo#ref shorthand; refused"
        )
    # npm aliases are handled by the caller (name translation), a bare alias
    # still contains "npm:" and was rejected above.
    # Whitespace inside a token would mean multiple specs glued together; the
    # range parser handles space separated comparators, but newlines/tabs in
    # odd places are tolerated by npm for || composition only.
    if any(ch in raw for ch in ("\x00",)):
        raise UnsupportedRangeError("dependency specification contains NUL bytes")


# A comparator: (operator, major, minor, patch, prerelease, x-mask flags).
# x-mask values are explicit for clarity in range expansion.


@dataclass(frozen=True)
class Comparator:
    op: str  # one of ">=", "<=", ">", "<", "="
    version: Optional[Version]  # None only for a pure "*" comparator (matches all)
    minor_is_x: bool = False
    patch_is_x: bool = False

    def satisfies(self, candidate: Version) -> bool:
        if self.version is None:
            return True
        if self.op == "=":
            if self.patch_is_x:
                if candidate.major != self.version.major:
                    return False
                if not self.minor_is_x and candidate.minor != self.version.minor:
                    return False
                # Prereleases on x-ranges: npm only allows prereleases of the
                # same [major,minor] tuple when the comparator itself is a
                # prerelease of that tuple.
                return _prerelease_compatible(candidate, self.version, self.minor_is_x)
            return candidate.compare(self.version) == 0
        return _numeric_compare(candidate, self.op, self.version)


def _prerelease_compatible(
    candidate: Version, comparator_version: Version, minor_is_x: bool
) -> bool:
    if not candidate.prerelease:
        return True
    if candidate.major != comparator_version.major:
        return False
    if minor_is_x:
        return True  # x on minor: any prerelease of that major
    return candidate.minor == comparator_version.minor


def _numeric_compare(candidate: Version, op: str, bound: Version) -> bool:
    result = candidate.compare(bound)
    if op == ">=":
        return result >= 0
    if op == "<=":
        return result <= 0
    if op == ">":
        return result > 0
    if op == "<":
        return result < 0
    raise SemverError(f"unknown operator {op!r}")  # pragma: no cover


@dataclass(frozen=True)
class ComparatorSet:
    comparators: Tuple[Comparator, ...]

    def satisfies(self, version: Version) -> bool:
        if not all(c.satisfies(version) for c in self.comparators):
            return False
        if version.prerelease:
            # npm rule: a prerelease version only satisfies a comparator set
            # if at least one comparator in that set has the same
            # [major,minor,patch] tuple *and* itself carries a prerelease tag.
            return any(
                c.version is not None
                and c.version.prerelease
                and (c.version.major, c.version.minor, c.version.patch)
                == (version.major, version.minor, version.patch)
                for c in self.comparators
            )
        return True


@dataclass(frozen=True)
class Range:
    raw: str
    sets: Tuple[ComparatorSet, ...]

    def satisfies(self, version: Union[str, Version]) -> bool:
        if isinstance(version, str):
            version = Version.parse(version)
        return any(cs.satisfies(version) for cs in self.sets)


# ---------------------------------------------------------------------------
# Range parsing
# ---------------------------------------------------------------------------

_HYPHEN_RE = re.compile(
    r"^\s*(?P<low>\S+)\s+-\s+(?P<high>\S+)\s*$"
)
_COMPARATOR_RE = re.compile(
    r"^\s*(?P<op>>=|<=|>|<|~>|~|\^|=|v?)?(?P<rest>.+?)\s*$"
)


def parse_range(spec: str) -> Range:
    """Parse a full npm range (OR of AND comparator-sets)."""
    if not isinstance(spec, str):
        raise UnsupportedRangeError("dependency range must be a string")
    reject_unsupported_spec(spec)
    raw = spec.strip()
    if raw in ("", "*", "x", "X", "latest"):
        # "latest" is a dist-tag, not a range: npm would resolve it at install
        # time against the registry. For an *offline gate* a floating tag means
        # the manifest does not pin anything, so refuse it.
        if raw == "latest":
            raise UnsupportedRangeError(
                "'latest' is a registry dist-tag, not a reproducible range"
            )
        # An empty range (some package.json files use "") is treated as "*"
        # by npm, matching any release.
        return Range(raw=raw, sets=(ComparatorSet((Comparator("=", None),)),))

    or_pieces = [piece.strip() for piece in raw.split("||")]
    sets: List[ComparatorSet] = []
    for piece in or_pieces:
        if not piece:
            raise UnsupportedRangeError(f"empty comparator-set in range {spec!r}")
        sets.append(_parse_comparator_set(piece, spec))
    return Range(raw=raw, sets=tuple(sets))


def _parse_comparator_set(piece: str, original: str) -> ComparatorSet:
    hyphen = _HYPHEN_RE.match(piece)
    if hyphen:
        low = _parse_partial_token(hyphen.group("low"), original)
        high = _parse_partial_token(hyphen.group("high"), original)
        return ComparatorSet((_lower_bound(low), _upper_bound_inclusive(high)))

    tokens = piece.split()
    if not tokens:
        raise UnsupportedRangeError(f"empty range segment in {original!r}")
    comparators: List[Comparator] = []
    for token in tokens:
        comparators.extend(_parse_token(token, original))
    return ComparatorSet(tuple(comparators))


@dataclass(frozen=True)
class _Partial:
    major: Union[int, str]
    minor: Union[int, str, None]
    patch: Union[int, str, None]
    prerelease: Tuple[Union[int, str], ...]


def _parse_partial_token(token: str, original: str) -> _Partial:
    match = _PARTIAL_RE.match(token)
    if not match:
        raise UnsupportedRangeError(
            f"cannot parse version token {token!r} in range {original!r}"
        )
    major = match.group("major")
    minor = match.group("minor")
    patch = match.group("patch")
    pre = tuple(_parse_prerelease(match.group("prerelease"), full=False))
    return _Partial(
        major="x" if major in (None, "x", "X", "*") else int(major),
        minor=None if minor is None else ("x" if minor in ("x", "X", "*") else int(minor)),
        patch=None if patch is None else ("x" if patch in ("x", "X", "*") else int(patch)),
        prerelease=pre,
    )


def _is_x(value: Union[int, str, None]) -> bool:
    return value is None or value == "x"


def _token_to_partial(token: str, original: str) -> tuple[_Partial, str]:
    """Split a leading operator off a token and parse its partial version."""
    operator_map = (">=", "<=", "~>", "^", ">", "<", "~", "=")
    op = ""
    rest = token
    if rest.startswith("v") or rest.startswith("V"):
        # "v1.2.3" is just a partial; "v" followed by non-digit is invalid.
        if len(rest) > 1 and rest[1].isdigit():
            rest = rest[1:]
    for candidate in operator_map:
        if rest.startswith(candidate):
            op = candidate
            rest = rest[len(candidate):]
            break
    if rest == "" and op in ("~", "~>", "^"):
        raise UnsupportedRangeError(f"range {original!r} has operator without version")
    partial = _parse_partial_token(rest if rest else "*", original)
    return partial, op


def _parse_token(token: str, original: str) -> List[Comparator]:
    partial, op = _token_to_partial(token, original)
    major_x = _is_x(partial.major)
    minor_x = _is_x(partial.minor)
    patch_x = _is_x(partial.patch)

    if major_x:
        if op not in ("", "="):
            raise UnsupportedRangeError(
                f"operator {op!r} on a wildcard version is invalid in {original!r}"
            )
        return [Comparator("=", None)]

    major = int(partial.major)  # type: ignore[arg-type]

    if op in ("~", "~>"):
        return _tilde(major, partial, minor_x, patch_x)
    if op == "^":
        return _caret(major, partial, minor_x, patch_x)
    if op in (">", ">=", "<", "<=", "="):
        return [_explicit_comparator(op, major, partial, minor_x, patch_x)]

    # Bare partial version:
    if minor_x:
        # "1" -> >=1.0.0 <2.0.0
        return [
            Comparator(">=", Version(major, 0, 0)),
            Comparator("<", Version(major + 1, 0, 0)),
        ]
    if patch_x:
        minor = int(partial.minor)  # type: ignore[arg-type]
        return [
            Comparator(">=", Version(major, minor, 0)),
            Comparator("<", Version(major, minor + 1, 0)),
        ]
    version = Version(
        major,
        int(partial.minor),  # type: ignore[arg-type]
        int(partial.patch),  # type: ignore[arg-type]
        partial.prerelease,
    )
    return [Comparator("=", version)]


def _explicit_comparator(
    op: str,
    major: int,
    partial: _Partial,
    minor_x: bool,
    patch_x: bool,
) -> Comparator:
    minor = 0 if partial.minor is None or partial.minor == "x" else int(partial.minor)
    patch = 0 if partial.patch is None or partial.patch == "x" else int(partial.patch)
    version = Version(major, minor, patch, partial.prerelease)
    if op == "=":
        return Comparator(
            "=",
            version,
            minor_is_x=minor_x,
            patch_is_x=patch_x,
        )
    if minor_x:
        # e.g. ">=1.x" behaves against the partial's floor/ceiling semantics.
        if op == ">=":
            return Comparator(">=", Version(major, 0, 0))
        if op == "<":
            return Comparator("<", Version(major + 1, 0, 0))
        if op == "<=":
            return Comparator("<", Version(major + 1, 0, 0))
        if op == ">":
            return Comparator(">=", Version(major + 1, 0, 0))
    if patch_x:
        if op == ">=":
            return Comparator(">=", Version(major, minor, 0))
        if op == "<":
            return Comparator("<", Version(major, minor + 1, 0))
        if op == "<=":
            return Comparator("<", Version(major, minor + 1, 0))
        if op == ">":
            return Comparator(">=", Version(major, minor + 1, 0))
    return Comparator(op, version)


def _lower_bound(partial: _Partial) -> Comparator:
    major = int(partial.major)  # type: ignore[arg-type]
    minor = 0 if _is_x(partial.minor) else int(partial.minor)  # type: ignore[arg-type]
    patch = 0 if _is_x(partial.patch) else int(partial.patch)  # type: ignore[arg-type]
    return Comparator(">=", Version(major, minor, patch, partial.prerelease))


def _upper_bound_inclusive(partial: _Partial) -> Comparator:
    major = int(partial.major)  # type: ignore[arg-type]
    if _is_x(partial.minor):
        return Comparator("<", Version(major + 1, 0, 0))
    minor = int(partial.minor)  # type: ignore[arg-type]
    if _is_x(partial.patch):
        return Comparator("<", Version(major, minor + 1, 0))
    patch = int(partial.patch)  # type: ignore[arg-type]
    return Comparator("<=", Version(major, minor, patch, partial.prerelease))


def _tilde(
    major: int, partial: _Partial, minor_x: bool, patch_x: bool
) -> List[Comparator]:
    if minor_x:
        return [
            Comparator(">=", Version(major, 0, 0)),
            Comparator("<", Version(major + 1, 0, 0)),
        ]
    minor = int(partial.minor)  # type: ignore[arg-type]
    if patch_x:
        return [
            Comparator(">=", Version(major, minor, 0)),
            Comparator("<", Version(major, minor + 1, 0)),
        ]
    patch = int(partial.patch)  # type: ignore[arg-type]
    return [
        Comparator(">=", Version(major, minor, patch, partial.prerelease)),
        Comparator("<", Version(major, minor + 1, 0)),
    ]


def _caret(
    major: int, partial: _Partial, minor_x: bool, patch_x: bool
) -> List[Comparator]:
    # ^ on partial versions fills missing fields with 0 for the lower bound.
    minor = 0 if minor_x else int(partial.minor)  # type: ignore[arg-type]
    patch = 0 if patch_x else int(partial.patch)  # type: ignore[arg-type]
    lower = Comparator(">=", Version(major, minor, patch, partial.prerelease))
    if major > 0 or minor_x:
        upper = Version(major + 1, 0, 0)
    elif minor > 0:
        upper = Version(0, minor + 1, 0)
    else:
        upper = Version(0, 0, patch + 1)
    return [lower, Comparator("<", upper)]


def satisfies(version: Union[str, Version], spec: str) -> bool:
    """Convenience wrapper used by tests and callers."""
    return parse_range(spec).satisfies(version)
