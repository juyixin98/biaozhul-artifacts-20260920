"""Maven subset: ComparableVersion token ordering + version ranges.

Concrete versions are split into numeric and qualifier tokens following
Maven's ``ComparableVersion`` rules: numeric tokens compare numerically,
qualifiers compare by the canonical Maven alias table (``alpha`` < ``beta``
< ``milestone`` < ``rc``/``cr`` < ``snapshot`` < "" (release) < ``sp``),
unknown qualifiers sort lexicographically *after* the release marker but
before ``sp``. List padding with zero tokens matches Maven behaviour.

Ranges support ``[1.0,2.0)`` style intervals (inclusive/exclusive),
unbounded sides, exact ``[1.5]`` and OR-disjoint ``[1.0,1.5),[2.0,)``.
A bare version is treated as a "soft requirement" meaning that exact
version (documented subset choice).
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from .errors import VersionError

_QUALIFIER_ORDER = {
    "alpha": 0, "a": 0,
    "beta": 1, "b": 1,
    "milestone": 2, "m": 2,
    "rc": 3, "cr": 3,
    "snapshot": 4,
    "": 5,          # release
    "ga": 5, "final": 5, "release": 5,
    "sp": 6,
}
# unknown qualifiers sort lexically between release(5) and sp(6)
_UNKNOWN_BASE = 5

_TOKEN_SPLIT_RE = re.compile(r"[._-]")


@dataclass(frozen=True)
class MavenVersion:
    tokens: tuple  # tuple of int | str; "" marks the release pad
    raw: str

    @property
    def is_prerelease(self) -> bool:
        return any(not isinstance(t, int) and _QUALIFIER_ORDER.get(t, _UNKNOWN_BASE) < _QUALIFIER_ORDER[""]
                   for t in self.tokens)


def _tokenize(segment: str) -> list:
    out: list = []
    buf = ""
    is_digit = segment[:1].isdigit() if segment else False
    for ch in segment:
        if ch.isdigit() == is_digit:
            buf += ch
        else:
            out.append(int(buf) if is_digit else buf)
            buf = ch
            is_digit = ch.isdigit()
    if buf:
        out.append(int(buf) if is_digit else buf)
    return out


def parse_maven(text: str) -> MavenVersion:
    if not isinstance(text, str):
        raise VersionError(f"version must be a string, got {type(text).__name__}")
    t = text.strip().strip("[]")
    if not t:
        raise VersionError("empty maven version")
    tokens: list = []
    for segment in _TOKEN_SPLIT_RE.split(t):
        if segment == "":
            continue
        tokens.extend(_tokenize(segment.lower()))
    if not tokens:
        raise VersionError(f"invalid maven version: {text!r}")
    # Maven pads trailing numeric zero / null tokens; normalization happens
    # at compare time.
    return MavenVersion(tuple(tokens), text.strip())


def _qualifier_key(q: str):
    if q in _QUALIFIER_ORDER:
        return (0, _QUALIFIER_ORDER[q], "")
    return (1, _UNKNOWN_BASE, q)


def _compare_token(a, b) -> int:
    if isinstance(a, int) and isinstance(b, int):
        return (a > b) - (a < b)
    if isinstance(a, int):
        # numeric vs qualifier: Maven treats a missing pad as numeric 0;
        # numeric segment vs string qualifier -> numbers first
        return 1 if isinstance(b, str) else -1
    if isinstance(b, int):
        return -1
    ka, kb = _qualifier_key(a), _qualifier_key(b)
    if ka < kb:
        return -1
    if ka > kb:
        return 1
    return 0


def _compare(a: MavenVersion, b: MavenVersion) -> int:
    # Maven pads the shorter list depending on the *other* side's next item:
    # a numeric counterpart is padded with 0, a qualifier with the release
    # marker "" (so 1.sp > 1 but 1-alpha < 1).
    n = max(len(a.tokens), len(b.tokens))
    for i in range(n):
        x = a.tokens[i] if i < len(a.tokens) else (0 if i < len(b.tokens) and isinstance(b.tokens[i], int) else "")
        y = b.tokens[i] if i < len(b.tokens) else (0 if i < len(a.tokens) and isinstance(a.tokens[i], int) else "")
        c = _compare_token(x, y)
        if c:
            return c
    return 0


# ----------------------------------------------------------------- ranges

@dataclass(frozen=True)
class _Bound:
    inclusive: bool
    version: MavenVersion


@dataclass(frozen=True)
class Interval:
    lower: _Bound | None
    upper: _Bound | None


_RANGE_CHARS_RE = re.compile(r"^[\[\]\(\),0-9a-zA-Z._\-]+$")


def _parse_bound(text: str, default_inclusive: bool) -> _Bound:
    text = text.strip()
    if not text:
        raise VersionError("empty range bound")
    return _Bound(default_inclusive, parse_maven(text))


def _parse_interval(expr: str) -> Interval:
    expr = expr.strip()
    if expr.startswith("[") and expr.endswith("]") and "," not in expr:
        v = parse_maven(expr[1:-1])
        return Interval(_Bound(True, v), _Bound(True, v))
    if not ((expr.startswith("[") or expr.startswith("("))
            and (expr.endswith("]") or expr.endswith(")"))):
        raise VersionError(f"invalid maven range interval: {expr!r}")
    lower_inclusive = expr.startswith("[")
    upper_inclusive = expr.endswith("]")
    inner = expr[1:-1]
    if inner.count(",") != 1:
        raise VersionError(f"maven range interval needs exactly one comma: {expr!r}")
    lo, hi = inner.split(",")
    lower = _parse_bound(lo, True) if lo.strip() else None
    upper = _parse_bound(hi, False) if hi.strip() else None
    if upper is not None:
        upper = _Bound(upper_inclusive, upper.version)
    if lower is not None:
        lower = _Bound(lower_inclusive, lower.version)
    return Interval(lower, upper)


def parse_maven_range(expr: str) -> tuple[Interval, ...]:
    expr = expr.strip()
    if not expr:
        raise VersionError("empty maven range")
    if not expr.startswith(("[", "(")):
        # soft requirement: exact version
        v = parse_maven(expr)
        return (Interval(_Bound(True, v), _Bound(True, v)),)
    if not _RANGE_CHARS_RE.match(expr):
        raise VersionError(f"illegal characters in maven range: {expr!r}")
    intervals = []
    depth = 0
    start = 0
    for i, ch in enumerate(expr):
        if ch in "[(":
            if depth == 0:
                start = i
            depth += 1
        elif ch in "])":
            depth -= 1
            if depth == 0:
                intervals.append(_parse_interval(expr[start:i + 1]))
        elif ch == "," and depth == 0:
            continue
    if depth != 0 or not intervals:
        raise VersionError(f"unbalanced maven range: {expr!r}")
    return tuple(intervals)


def _in_interval(v: MavenVersion, iv: Interval) -> bool:
    if iv.lower is not None:
        c = _compare(v, iv.lower.version)
        if c < 0 or (c == 0 and not iv.lower.inclusive):
            return False
    if iv.upper is not None:
        c = _compare(v, iv.upper.version)
        if c > 0 or (c == 0 and not iv.upper.inclusive):
            return False
    return True


def maven_satisfies(version: str, range_expr: str) -> bool:
    v = parse_maven(version)
    return any(_in_interval(v, iv) for iv in parse_maven_range(range_expr))
