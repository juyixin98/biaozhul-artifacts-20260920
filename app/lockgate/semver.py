"""Self-contained SemVer 2.0 parser and range evaluator.

Only npm's documented semver range grammar is implemented. Anything outside
that grammar (URLs, git specs, tags, ``file:``, unknown operators, exotic
build metadata comparisons, ...) is rejected explicitly by raising
:class:`UnsupportedSpecError`, never silently coerced.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache
from typing import List, Optional, Tuple


class UnsupportedSpecError(ValueError):
    """Raised when a specifier uses syntax this gate refuses to interpret."""


# ---- Version -------------------------------------------------------------

_VERSION_RE = re.compile(
    r"^v?(?P<major>0|[1-9]\d*)"
    r"\.(?P<minor>0|[1-9]\d*)"
    r"\.(?P<patch>0|[1-9]\d*)"
    r"(?:-(?P<pre>[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?"
    r"(?:\+(?P<build>[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?$"
)


@dataclass(frozen=True, order=True)
class Version:
    major: int
    minor: int
    patch: int
    prerelease: Tuple = ()
    # build metadata exists for identity display but, per SemVer, never
    # participates in precedence.
    build: str = ""

    @property
    def is_prerelease(self) -> bool:
        return bool(self.prerelease)

    def __str__(self) -> str:
        s = f"{self.major}.{self.minor}.{self.patch}"
        if self.prerelease:
            s += "-" + ".".join(str(p) for p in self.prerelease)
        if self.build:
            s += "+" + self.build
        return s


def _pre_tuple(pre: Optional[str]) -> Tuple:
    if not pre:
        return ()
    out = []
    for ident in pre.split("."):
        if ident.isdigit():
            if len(ident) > 1 and ident[0] == "0":
                raise UnsupportedSpecError(f"numeric prerelease identifier has leading zero: {ident}")
            out.append((0, int(ident)))
        else:
            out.append((1, ident))
    return tuple(out)


def parse_version(text: str) -> Version:
    m = _VERSION_RE.match(text.strip())
    if not m:
        raise UnsupportedSpecError(f"not a valid semver version: {text!r}")
    major = int(m.group("major"))
    minor = int(m.group("minor") or "0")
    patch = int(m.group("patch") or "0")
    return Version(major, minor, patch, _pre_tuple(m.group("pre")), m.group("build") or "")


# ---- Comparators ---------------------------------------------------------

# Operators recognised. Anything else is an explicit refusal.
_OPS = ("<=", ">=", "==", "<", ">", "=")


@dataclass(frozen=True)
class Comparator:
    op: str
    version: Version

    def satisfies(self, v: Version) -> bool:
        c = _cmp(v, self.version)
        if self.op in ("==", "="):
            return c == 0
        if self.op == ">":
            return c > 0
        if self.op == ">=":
            return c >= 0
        if self.op == "<":
            return c < 0
        if self.op == "<=":
            return c <= 0
        raise UnsupportedSpecError(self.op)


def _cmp(a: Version, b: Version) -> int:
    for x, y in ((a.major, b.major), (a.minor, b.minor), (a.patch, b.patch)):
        if x != y:
            return -1 if x < y else 1
    if a.prerelease == b.prerelease:
        return 0
    if not a.prerelease:
        return 1  # 1.0.0 > 1.0.0-alpha
    if not b.prerelease:
        return -1
    for x, y in zip(a.prerelease, b.prerelease):
        if x == y:
            continue
        # (0,int) sorts below (1,str); compare within same tag kind directly
        if x[0] != y[0]:
            return -1 if x[0] < y[0] else 1
        return -1 if x[1] < y[1] else 1
    return -1 if len(a.prerelease) < len(b.prerelease) else 1


# ---- Range parsing -------------------------------------------------------

_PARTIAL = re.compile(
    r"^v?(\d+|[xX*])(?:\.(\d+|[xX*]))?(?:\.(\d+|[xX*]))?"
    r"(?:-([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?(?:\+[0-9A-Za-z-]+)?$"
)

# Tokens that prove a spec is not a plain semver range. These are reported
# as unsupported rather than guessed.
_FORBIDDEN = ("file:", "git:", "git+", "github:", "http://", "https://",
              "npm:", "link:", "workspace:", "portal:", "github.com/",
              "gitlab:", "bitbucket:", "latest")


def _check_forbidden(text: str) -> None:
    low = text.strip()
    if low in ("*", "x", "X", ""):
        return
    for token in _FORBIDDEN:
        if token in low:
            raise UnsupportedSpecError(f"unsupported non-registry or non-semver spec: {text!r} ({token.rstrip(':')})")
    # any github shorthand "org/repo"
    if re.match(r"^[\w.-]+/[\w.-]+(?:#.*)?$", low) and not low.startswith("@"):
        raise UnsupportedSpecError(f"unsupported GitHub shorthand spec: {text!r}")


def _expand_partial(token: str) -> List[Comparator]:
    """Expand X-ranges / partial versions to a comparator pair.

    Returns the conjunction that a concrete version must satisfy.
    """
    t = token.strip()
    if t in ("*", "x", "X", ""):
        return []  # matches everything non-prerelease (handled at comparator level)

    # Caret / tilde handled by callers; here only partial versions may arrive.
    # partial versions allow x/X/* wildcards on minor/patch
    m = _PARTIAL.match(t.lstrip("vV"))
    if not m:
        raise UnsupportedSpecError(f"unparseable version token: {token!r}")
    maj_s, min_s, pat_s, pre = m.groups()
    if maj_s in ("x", "X", "*"):
        return []
    has_min = min_s is not None and min_s not in ("x", "X", "*")
    has_pat = pat_s is not None and pat_s not in ("x", "X", "*")

    if not has_min:
        # X-range: 2.x  => >=2.0.0 <3.0.0 ; 0.x => >=0.0.0 <1.0.0
        low = Version(int(maj_s), 0, 0)
        high = Version(int(maj_s) + 1, 0, 0)
        return [Comparator(">=", low), Comparator("<", high)]
    if not has_pat:
        low = Version(int(maj_s), int(min_s), 0)
        high = Version(int(maj_s), int(min_s) + 1, 0)
        return [Comparator(">=", low), Comparator("<", high)]
    low = Version(int(maj_s), int(min_s), int(pat_s), _pre_tuple(pre))
    return [Comparator(">=", low), Comparator("<=", Version(low.major, low.minor, low.patch, low.prerelease))]


def _caret(token: str) -> List[Comparator]:
    body = token[1:].lstrip()
    m = _PARTIAL.match(body.lstrip("vV"))
    if not m:
        raise UnsupportedSpecError(f"unparseable caret range: {token!r}")
    maj_s, min_s, pat_s, pre = m.groups()
    if min_s is None:
        min_s = "x"
    if pat_s is None:
        pat_s = "x"
    x_min = min_s in ("x", "X", "*")
    x_pat = pat_s in ("x", "X", "*")
    M = int(maj_s)
    m_ = 0 if x_min else int(min_s)
    p = 0 if x_pat else int(pat_s)
    pre_t = _pre_tuple(None if (x_min or x_pat) else pre)
    low = Version(M, m_, p, pre_t)

    if M > 0:
        high = Version(M + 1, 0, 0)
    elif x_min:
        # ^0 / ^0.x => <1.0.0
        high = Version(1, 0, 0)
    elif m_ > 0:
        high = Version(0, m_ + 1, 0)
    else:
        high = Version(0, 0, p + 1)
    return [Comparator(">=", low), Comparator("<", high)]


def _tilde(token: str) -> List[Comparator]:
    body = token[1:].lstrip()
    m = _PARTIAL.match(body.lstrip("vV"))
    if not m:
        raise UnsupportedSpecError(f"unparseable tilde range: {token!r}")
    maj_s, min_s, pat_s, pre = m.groups()
    has_min = min_s is not None and min_s not in ("x", "X", "*")
    M = int(maj_s)
    if not has_min:
        return [Comparator(">=", Version(M, 0, 0)), Comparator("<", Version(M + 1, 0, 0))]
    m_ = int(min_s)
    if pat_s is None or pat_s in ("x", "X", "*"):
        return [Comparator(">=", Version(M, m_, 0)), Comparator("<", Version(M, m_ + 1, 0))]
    v = Version(M, m_, int(pat_s), _pre_tuple(pre))
    return [Comparator(">=", v), Comparator("<", Version(M, m_ + 1, 0))]


def _hyphen_range(a: str, b: str) -> List[Comparator]:
    la = _expand_partial(a)
    lb = _expand_partial(b)
    low = min(c.version for c in la if c.op == ">=")
    # upper end: inclusive if b is fully specified
    mb = _PARTIAL.match(b.strip().lstrip("vV"))
    full = mb and mb.group(2) not in (None, "x", "X", "*") and mb.group(3) not in (None, "x", "X", "*")
    if full:
        high_v = parse_version(b.strip().lstrip("vV"))
        return [Comparator(">=", low), Comparator("<=", high_v)]
    high_cmps = [c for c in lb if c.op == "<"]
    high = max(c.version for c in high_cmps)
    return [Comparator(">=", low), Comparator("<", high)]


def _parse_comparator_set(text: str) -> List[Comparator]:
    text = text.strip()
    if not text:
        return []
    # hyphen range
    hy = re.split(r"\s+-\s+", text)
    if len(hy) == 2:
        return _hyphen_range(hy[0], hy[1])
    if len(hy) > 2:
        raise UnsupportedSpecError(f"malformed hyphen range: {text!r}")

    tokens = text.split()
    out: List[Comparator] = []
    # split joined comparators like ">=1.0.0<2.0.0" while leaving
    # whitespace-free tokens such as "1.2.x" intact.
    joined: List[str] = []
    for tok in tokens:
        parts = re.findall(
            r"(?:\^|~|>=|<=|==|>|<|=)?v?[0-9xX*][0-9xX*.\-+A-Za-z]*",
            tok,
        )
        joined.extend(parts if parts else [tok])
    for tok in joined:
        tok = tok.strip()
        if not tok:
            continue
        if tok.startswith("^"):
            out.extend(_caret(tok))
        elif tok.startswith("~"):
            out.extend(_tilde(tok))
        elif tok[:2] in (">=", "<=", "=="):
            out.append(Comparator(tok[:2], parse_version(tok[2:])))
        elif tok[0] in "<>=":
            op = "=" if tok[0] == "=" else tok[0]
            out.append(Comparator(op, parse_version(tok[1:])))
        else:
            out.extend(_expand_partial(tok))
    return out


@dataclass(frozen=True)
class Range:
    raw: str
    sets: Tuple[Tuple[Comparator, ...], ...]

    def satisfies(self, version_text: str) -> bool:
        v = parse_version(version_text)
        for comparators in self.sets:
            if not all(c.satisfies(v) for c in comparators):
                continue
            # node-semver prerelease rule: a candidate prerelease is only
            # accepted when the comparator set explicitly carries a
            # prerelease whose [major,minor,patch] tuple equals the
            # candidate's own tuple.
            if v.is_prerelease:
                tuple_ = (v.major, v.minor, v.patch)
                touched = {(c.version.major, c.version.minor, c.version.patch)
                           for c in comparators if c.version.is_prerelease}
                if tuple_ not in touched:
                    continue
            return True
        return False


@lru_cache(maxsize=4096)
def parse_range(text: str) -> Range:
    raw = (text or "").strip()
    _check_forbidden(raw)
    # strip leading '=' ('=1.2.3' == '1.2.3') but only when not '=='
    if raw.startswith("=") and not raw.startswith("=="):
        raw = raw[1:].strip()
    # OR sets
    or_parts = [p for p in re.split(r"\s*\|\|\s*", raw)]
    sets = []
    for part in or_parts:
        part = part.strip()
        # ignore trailing "||" meaning or anything (e.g. "1.0.0 ||")
        if not part:
            sets.append(())
            continue
        sets.append(tuple(_parse_comparator_set(part)))
    return Range(raw=text.strip(), sets=tuple(sets))


def satisfies(version_text: str, range_text: str) -> bool:
    """Real semver satisfaction check; raises UnsupportedSpecError on bad syntax."""
    return parse_range(range_text).satisfies(version_text)
