"""SemVer subset with npm-style range semantics.

Concrete versions are parsed loosely (npm accepts ``1.2`` and ``1``),
then compared by the numeric (major, minor, patch) tuple and by SemVer
2.0.0 pre-release precedence — never as strings.

Supported range syntax
-----------------------
* comparators: ``< <= > >= =`` plus exact versions
* x-ranges: ``1``, ``1.2``, ``1.x``, ``*``
* tilde / caret: ``~1.2.3``, ``^1.2.3`` (incl. ``^0.x`` special cases)
* hyphen ranges: ``1.2.3 - 2.3.4``
* AND (space-separated comparators) and OR (``||``)
* pre-release versions on both sides, with npm's pre-release tag rule:
  a pre-release version only satisfies a range if the range mentions a
  comparator with the same [major, minor, patch] tuple.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from .errors import VersionError

_VERSION_RE = re.compile(
    r"^v?(\d+)(?:\.(\d+))?(?:\.(\d+))?(?:-([0-9A-Za-z.-]+))?(?:\+[0-9A-Za-z.-]+)?$"
)
_PRERELEASE_PART_RE = re.compile(r"^[0-9A-Za-z-]+$")


@dataclass(frozen=True)
class SemVer:
    major: int
    minor: int
    patch: int
    prerelease: tuple  # tuple of int | str; empty tuple == release
    raw: str

    @property
    def is_prerelease(self) -> bool:
        return bool(self.prerelease)

    def core(self) -> tuple[int, int, int]:
        return (self.major, self.minor, self.patch)


def _prerelease_key(parts: list[str]):
    key = []
    for part in parts:
        if part.isdigit():
            key.append((0, int(part)))  # numeric identifiers have lower precedence
        else:
            key.append((1, part))
    return tuple(key)


def parse_semver(text: str) -> SemVer:
    if not isinstance(text, str):
        raise VersionError(f"version must be a string, got {type(text).__name__}")
    m = _VERSION_RE.match(text.strip())
    if not m:
        raise VersionError(f"invalid semver version: {text!r}")
    major_s, minor_s, patch_s, pre = m.groups()
    major = int(major_s)
    minor = int(minor_s) if minor_s is not None else 0
    patch = int(patch_s) if patch_s is not None else 0
    prerelease: tuple = ()
    if pre is not None:
        if not pre or not all(_PRERELEASE_PART_RE.match(p) for p in pre.split(".")):
            raise VersionError(f"invalid semver prerelease: {text!r}")
        prerelease = _prerelease_key(pre.split("."))
    return SemVer(major, minor, patch, prerelease, text.strip())


def _compare(a: SemVer, b: SemVer) -> int:
    for x, y in ((a.major, b.major), (a.minor, b.minor), (a.patch, b.patch)):
        if x != y:
            return -1 if x < y else 1
    if not a.prerelease and not b.prerelease:
        return 0
    if not a.prerelease:
        return 1  # release > prerelease
    if not b.prerelease:
        return -1
    if a.prerelease < b.prerelease:
        return -1
    if a.prerelease > b.prerelease:
        return 1
    return 0


# ---------------------------------------------------------------- ranges

@dataclass(frozen=True)
class _Op:
    op: str           # one of >= <= > < =
    version: SemVer
    partial: bool = False  # expanded from an x-range / truncated version

    @property
    def tuple(self):
        return self.version.core()


def _parse_partial(token: str) -> tuple[int | None, SemVer | None, str]:
    """Parse an exact / x-range / truncated version token.

    Returns (precision, version, raw) where precision is the number of
    fully-specified core components (0/1/2/3): ``*`` -> 0, ``1`` -> 1,
    ``1.2`` -> 2, ``1.2.3`` -> 3.
    """
    t = token.strip()
    if t in ("", "*", "x", "X", "latest"):
        return 0, None, t
    m = re.match(r"^v?(\d+|[xX*])(?:\.(\d+|[xX*]))?(?:\.(\d+|[xX*]))?(?:-([0-9A-Za-z.-]+))?(?:\+[0-9A-Za-z.-]+)?$", t)
    if not m:
        raise VersionError(f"invalid version range token: {token!r}")
    maj, minr, pat, pre = m.groups()
    if maj in ("x", "X", "*"):
        return 0, None, t
    M = int(maj)
    if minr is None or minr in ("x", "X", "*"):
        return 1, SemVer(M, 0, 0, (), t), t
    m0 = int(minr)
    if pat is None or pat in ("x", "X", "*"):
        return 2, SemVer(M, m0, 0, (), t), t
    p = int(pat)
    if pre is not None:
        return 3, parse_semver(t), t
    return 3, SemVer(M, m0, p, (), t), t


def _expand_partial(prec: int, ver: SemVer | None, raw: str):
    """Expand an x-range/truncated lower bound into concrete >=/< ops."""
    if prec == 0:
        return []  # unbounded
    if prec == 1:
        return [_Op(">=", SemVer(ver.major, 0, 0, (), raw)),
                _Op("<", SemVer(ver.major + 1, 0, 0, (), raw))]
    if prec == 2:
        return [_Op(">=", SemVer(ver.major, ver.minor, 0, (), raw)),
                _Op("<", SemVer(ver.major, ver.minor + 1, 0, (), raw))]
    return [_Op("=", ver)]


_COMPARATOR_RE = re.compile(r"^(>=|<=|>|<|=)?\s*(.+)$")


def _parse_comparator(token: str):
    """Return list of concrete _Op for one comparator group token."""
    token = token.strip()
    if not token:
        return []
    if token.startswith("^"):
        return _caret(token[1:].strip())
    if token.startswith("~"):
        return _tilde(token[1:].strip())
    m = _COMPARATOR_RE.match(token)
    op, rest = m.group(1), m.group(2).strip()
    op = op or "="
    prec, ver, raw = _parse_partial(rest)
    if prec < 3:
        return _expand_partial(prec, ver, raw)
    if op == "=":
        return [_Op("=", ver)]
    return [_Op(op, ver)]


def _tilde(ver: str):
    prec, v, raw = _parse_partial(ver)
    if prec < 3:
        return _expand_partial(prec, v, raw)
    # ~1.2.3 := >=1.2.3 <1.3.0 ; ~1.2 := >=1.2.0 <1.3.0 ; ~1 := >=1.0.0 <2.0.0
    m = re.match(r"^v?(\d+)(?:\.(\d+))?(?:\.(\d+))?", ver.strip())
    maj, minr, pat = m.groups()
    if minr is None:
        upper = SemVer(int(maj) + 1, 0, 0, (), ver)
    else:
        upper = SemVer(int(maj), int(minr) + 1, 0, (), ver)
    return [_Op(">=", v), _Op("<", upper)]


def _caret(ver: str):
    prec, v, raw = _parse_partial(ver)
    if prec < 3:
        return _expand_partial(prec, v, raw)
    M, m0, pat = v.major, v.minor, v.patch
    # caret: allow changes that do not modify the left-most non-zero element
    if M != 0:
        upper = SemVer(M + 1, 0, 0, (), ver)
    elif m0 != 0:
        upper = SemVer(0, m0 + 1, 0, (), ver)
    elif pat != 0:
        upper = SemVer(0, 0, pat + 1, (), ver)
    else:  # ^0.0.0
        upper = SemVer(0, 0, 1, (), ver)
    return [_Op(">=", v), _Op("<", upper)]


def _split_hyphen(expr: str) -> str:
    """Rewrite ``a - b`` hyphen ranges into comparator form."""
    def repl(m):
        lo, hi = m.group(1).strip(), m.group(2).strip()
        hi_prec, _, _ = _parse_partial(hi)
        if hi_prec < 3:
            bounds = _expand_partial(*_parse_partial(hi))
            upper = next((b for b in bounds if b.op == "<"), None)
            hi_text = f"<{upper.version.major}.{upper.version.minor}.{upper.version.patch}" if upper else ""
        else:
            hi_text = f"<={hi}"
        lo_text = ">=" + lo
        return f"{lo_text} {hi_text}".strip()
    return re.sub(r"(\S.*?)\s+-\s+(\S.*)", repl, expr)


@dataclass(frozen=True)
class ComparatorSet:
    ops: tuple  # tuple of _Op, AND-combined

    def mentions(self, core) -> bool:
        """npm prerelease rule: any comparator with the exact [M,m,p]?"""
        return any(not o.partial and o.version.core() == core for o in self.ops)


def _parse_comparator_set(text: str) -> ComparatorSet:
    text = _split_hyphen(text.strip())
    tokens = text.split()
    ops: list[_Op] = []
    for tok in tokens:
        ops.extend(_parse_comparator(tok))
    return ComparatorSet(tuple(ops))


def parse_range(expr: str):
    """Parse an npm range expression into a tuple of ComparatorSet (OR)."""
    if expr is None:
        raise VersionError("range expression is required")
    expr = expr.strip()
    if expr == "":
        raise VersionError("empty range expression")
    sets = []
    for part in expr.split("||"):
        part = part.strip()
        if part:
            sets.append(_parse_comparator_set(part))
    if not sets:
        raise VersionError(f"no comparator sets in range: {expr!r}")
    return tuple(sets)


def _check_op(v: SemVer, op: _Op) -> bool:
    c = _compare(v, op.version)
    if op.op == "=":
        return c == 0
    if op.op == ">":
        return c > 0
    if op.op == "<":
        return c < 0
    if op.op == ">=":
        return c >= 0
    if op.op == "<=":
        return c <= 0
    raise VersionError(f"unknown comparator {op.op!r}")  # pragma: no cover


def satisfies_set(v: SemVer, cset: ComparatorSet) -> bool:
    if not all(_check_op(v, op) for op in cset.ops):
        return False
    if v.is_prerelease and not cset.mentions(v.core()):
        return False
    return True


def semver_satisfies(version: str, range_expr: str) -> bool:
    v = parse_semver(version)
    return any(satisfies_set(v, cs) for cs in parse_range(range_expr))
