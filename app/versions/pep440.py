"""PEP 440 subset: public version ordering + version specifiers.

Implements real PEP 440 ordering (epoch / release / pre / post / dev,
numeric precedence — no string comparison) and the specifier operators
``== != > >= < <= ~= ===``, wildcard ``==1.0.*`` and comma-AND sets.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from .errors import VersionError

_PRE_ALIASES = {"a": "a", "alpha": "a", "b": "b", "beta": "b",
                "c": "rc", "rc": "rc", "cr": "rc",
                "pre": "rc", "preview": "rc"}
_PRE_RANK = {"a": 0, "b": 1, "rc": 2}

_VERSION_RE = re.compile(
    r"""
    ^\s*v?
    (?:(?P<epoch>[0-9]+)!)?
    (?P<release>[0-9]+(?:\.[0-9]+)*)
    (?:[._-]?(?P<pre>alpha|beta|preview|pre|rc|cr|c|b|a|alpha|b|a)[._-]?(?P<pre_n>[0-9]+)?)?
    (?:[._-]?post[._-]?(?P<post>[0-9]+))?
    (?:[._-]?dev[._-]?(?P<dev>[0-9]+))?
    (?:\+(?P<local>[a-z0-9]+(?:[._-][a-z0-9]+)*))?
    \s*$
    """,
    re.VERBOSE | re.IGNORECASE,
)


@dataclass(frozen=True)
class PEP440Version:
    epoch: int
    release: tuple[int, ...]
    pre: tuple[str, int] | None
    post: int | None
    dev: int | None
    local: str | None
    raw: str

    @property
    def is_prerelease(self) -> bool:
        return self.pre is not None or self.dev is not None

    @property
    def public(self) -> str:
        return self.raw.split("+", 1)[0].strip()


def parse_pep440(text: str) -> PEP440Version:
    if not isinstance(text, str):
        raise VersionError(f"version must be a string, got {type(text).__name__}")
    m = _VERSION_RE.match(text)
    if not m:
        raise VersionError(f"invalid PEP 440 version: {text!r}")
    epoch = int(m.group("epoch") or 0)
    release = tuple(int(x) for x in m.group("release").split("."))
    pre = None
    if m.group("pre"):
        letter = _PRE_ALIASES[m.group("pre").lower()]
        pre = (letter, int(m.group("pre_n") or 0))
    post = int(m.group("post")) if m.group("post") is not None else None
    dev = int(m.group("dev")) if m.group("dev") is not None else None
    return PEP440Version(epoch, release, pre, post, dev,
                         m.group("local"), text.strip())


def _order_key(v: PEP440Version):
    release = v.release + (0,) * (4 - len(v.release)) if len(v.release) < 4 else v.release
    if v.dev is not None and v.pre is None and v.post is None:
        pre_seg = (-1, 0, 0)          # bare dev release: before any alpha
    elif v.pre is not None:
        pre_seg = (0, _PRE_RANK[v.pre[0]], v.pre[1])
    elif v.post is not None:
        pre_seg = (2, v.post, 0)
    else:
        pre_seg = (1, 0, 0)          # final release
    dev_seg = (0, v.dev) if v.dev is not None else (1, 0)
    return (v.epoch, release, pre_seg, dev_seg)


def _compare(a: PEP440Version, b: PEP440Version) -> int:
    ka, kb = _order_key(a), _order_key(b)
    if ka < kb:
        return -1
    if ka > kb:
        return 1
    return 0


# ------------------------------------------------------------- specifiers

@dataclass(frozen=True)
class _Spec:
    op: str
    version: PEP440Version | None   # None for === (raw compare)
    raw_version: str
    wildcard: bool = False

    @property
    def mentions_prerelease(self) -> bool:
        return self.version is not None and self.version.is_prerelease


def _candidate_for_compare(v: PEP440Version, spec: _Spec) -> PEP440Version:
    # PEP 440: local segment is ignored when the specifier has no local part.
    if spec.version is not None and spec.version.local is None and v.local is not None:
        return PEP440Version(v.epoch, v.release, v.pre, v.post, v.dev, None, v.public)
    return v


def _prefix_match(v: PEP440Version, spec: _Spec) -> bool:
    """Wildcard equality: release must start with spec's release tuple."""
    prefix = spec.version.release
    if v.epoch != spec.version.epoch:
        return False
    if len(v.release) < len(prefix):
        return v.release + (0,) * (len(prefix) - len(v.release)) == prefix
    return v.release[: len(prefix)] == prefix


def _check(v: PEP440Version, spec: _Spec) -> bool:
    if spec.op == "===":
        return v.public == spec.raw_version.strip()
    candidate = _candidate_for_compare(v, spec)
    if spec.op == "==":
        if spec.wildcard:
            return _prefix_match(candidate, spec)
        return _compare(candidate, spec.version) == 0
    if spec.op == "!=":
        if spec.wildcard:
            return not _prefix_match(candidate, spec)
        return _compare(candidate, spec.version) != 0
    c = _compare(candidate, spec.version)
    if spec.op == ">":
        return c > 0
    if spec.op == "<":
        return c < 0
    if spec.op == ">=":
        return c >= 0
    if spec.op == "<=":
        return c <= 0
    raise VersionError(f"unknown specifier {spec.op!r}")  # pragma: no cover


_SPEC_RE = re.compile(r"(===|==|!=|~=|>=|<=|>|<)\s*([^,]+)")


def _parse_spec(text: str) -> _Spec:
    m = _SPEC_RE.match(text.strip())
    if not m:
        raise VersionError(f"invalid PEP 440 specifier: {text!r}")
    op, ver_text = m.group(1), m.group(2).strip()
    if op == "===":
        return _Spec(op, None, ver_text)
    wildcard = ver_text.endswith(".*")
    if wildcard:
        if op not in ("==", "!="):
            raise VersionError(f"wildcard only valid with ==/!=: {text!r}")
        ver_text = ver_text[:-2]
    ver = parse_pep440(ver_text)
    if wildcard and (ver.pre is not None or ver.post is not None or ver.dev is not None
                     or ver.local is not None):
        raise VersionError(f"wildcard prefix must be a release version: {text!r}")
    if op == "~=":
        if len(ver.release) < 2 or wildcard:
            raise VersionError(f"~= needs at least two release segments: {text!r}")
    return _Spec(op, ver, ver_text, wildcard)


def parse_specifier_set(expr: str):
    """Parse comma-AND specifier set into ((lower/upper checks), prerelease?).

    Returns tuple of _Spec; ``~=`` is expanded into >= and < constraints.
    """
    if not expr or not expr.strip():
        raise VersionError("empty PEP 440 specifier set")
    specs: list[_Spec] = []
    for part in expr.split(","):
        part = part.strip()
        if not part:
            continue
        spec = _parse_spec(part)
        if spec.op != "~=":
            specs.append(spec)
            continue
        v = spec.version
        # ~=1.2.3 -> >=1.2.3,<1.3 ; ~=1.2 -> >=1.2,<2
        bound = v.release[:-1]
        upper = PEP440Version(v.epoch, bound[:-1] + (bound[-1] + 1,), None, None, None, None, "")
        lower = PEP440Version(v.epoch, v.release, v.pre, v.post, v.dev, v.local, v.raw)
        specs.append(_Spec(">=", lower, spec.raw_version))
        specs.append(_Spec("<", upper, spec.raw_version))
    if not specs:
        raise VersionError(f"no specifiers in: {expr!r}")
    return tuple(specs)


def pep440_satisfies(version: str, range_expr: str) -> bool:
    v = parse_pep440(version)
    specs = parse_specifier_set(range_expr)
    if not all(_check(v, s) for s in specs):
        return False
    # PEP 440 pre-release exclusion: a pre-release only matches a set that
    # explicitly mentions a pre-release boundary.
    if v.is_prerelease and not any(s.mentions_prerelease for s in specs):
        return False
    return True
