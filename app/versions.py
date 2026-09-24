"""按生态规则进行的真实版本解析与范围比较。

支持的生态 / 规则：

* ``npm``  —— SemVer 2.0 解析 + npm 范围语法（``^``/``~``/``x`` 范围/
  ``-`` 区间/``||`` 并集/比较器交集），含 npm 预发布门禁规则。
* ``maven`` —— Maven 版本顺序（数字段 vs 限定词：alpha<beta<milestone<
  rc<snapshot<'' (release)<sp），范围采用 ``[1.0,2.0)`` 区间语法。
* ``pypi``  —— PEP 440（``packaging`` 库的真实实现），``>=,<,~=,==,!=``。
* ``gem``   —— RubyGems 分段版本顺序与 ``~>`` pessimistic 约束。
* ``deb``   —— dpkg 版本比较（epoch:upstream-revision，``~`` 排序、
  字母先于非字母），比较器集合语法。

所有比较都基于解析后的结构化版本号，**绝不做字符串大小比较**。
解析失败抛 :class:`VersionError`，由上层映射为「未知」。
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable

from packaging.specifiers import SpecifierSet
from packaging.version import InvalidVersion, Version


class VersionError(ValueError):
    """版本字符串无法按该生态规则解析。"""


class UnsupportedEcosystemError(ValueError):
    """该生态没有可用的版本比较器。"""


# ---------------------------------------------------------------------------
# npm / SemVer
# ---------------------------------------------------------------------------

_SEMVER_RE = re.compile(
    r"^(?P<major>0|[1-9]\d*)"
    r"\.(?P<minor>0|[1-9]\d*)"
    r"\.(?P<patch>0|[1-9]\d*)"
    r"(?:-(?P<pre>[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?"
    r"(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?$"
)
_PARTIAL_RE = re.compile(
    r"^[vV]?(\d+|[xX*])(?:\.(\d+|[xX*]))?(?:\.(\d+|[xX*]))?"
    r"(?:-([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?$"
)
_PRE_IDENT_RE = re.compile(r"^[0-9A-Za-z-]+$")


@dataclass(frozen=True)
class SemVer:
    major: int
    minor: int
    patch: int
    pre: tuple[str | int, ...] | None = None  # None = 正式版

    @staticmethod
    def parse(text: str) -> "SemVer":
        m = _SEMVER_RE.match(text.strip())
        if not m:
            raise VersionError(f"not a valid semver version: {text!r}")
        pre = _parse_pre(m.group("pre"))
        return SemVer(int(m.group("major")), int(m.group("minor")),
                      int(m.group("patch")), pre)

    def core(self) -> tuple[int, int, int]:
        return self.major, self.minor, self.patch

    def cmp_key(self):
        # 无预发布 > 有预发布：用 1/0 标志位
        return (self.major, self.minor, self.patch,
                1 if self.pre is None else 0,
                self.pre or ())


def _parse_pre(text: str | None) -> tuple[str | int, ...] | None:
    if text is None:
        return None
    idents: list[str | int] = []
    for ident in text.split("."):
        if not ident or not _PRE_IDENT_RE.match(ident):
            raise VersionError(f"invalid semver prerelease identifier: {ident!r}")
        if ident.isdigit():
            if len(ident) > 1 and ident[0] == "0":
                raise VersionError(f"numeric prerelease identifier has leading zero: {ident!r}")
            idents.append(int(ident))
        else:
            idents.append(ident)
    return tuple(idents)


def _cmp_pre(a: tuple, b: tuple) -> int:
    for x, y in zip(a, b):
        if type(x) is int and type(y) is int:
            if x != y:
                return -1 if x < y else 1
        elif type(x) is int:
            return -1  # 数字标识符优先级低于非数字
        elif type(y) is int:
            return 1
        elif x != y:
            return -1 if x < y else 1
    if len(a) == len(b):
        return 0
    return -1 if len(a) < len(b) else 1


def semver_compare(a: SemVer, b: SemVer) -> int:
    ka, kb = a.cmp_key(), b.cmp_key()
    if ka[:3] != kb[:3]:
        return -1 if ka[:3] < kb[:3] else 1
    if a.pre is None and b.pre is None:
        return 0
    if a.pre is None:
        return 1
    if b.pre is None:
        return -1
    return _cmp_pre(a.pre, b.pre)  # type: ignore[arg-type]


@dataclass(frozen=True)
class _Partial:
    major: int | None
    minor: int | None
    patch: int | None
    pre: tuple[str | int, ...] | None


def _parse_partial(token: str) -> _Partial:
    m = _PARTIAL_RE.match(token.strip())
    if not m:
        raise VersionError(f"unparseable semver token: {token!r}")

    def conv(g: str | None) -> int | None:
        if g is None or g in ("x", "X", "*"):
            return None
        return int(g)

    parts = (conv(m.group(1)), conv(m.group(2)), conv(m.group(3)))
    if parts[1] is None:
        major, minor, patch = parts[0], None, None
    elif parts[2] is None:
        major, minor, patch = parts[0], parts[1], None
    else:
        major, minor, patch = parts
    if major is None:
        return _Partial(None, None, None, None)
    return _Partial(major, minor, patch, _parse_pre(m.group(4)))


def _sv(major: int, minor: int = 0, patch: int = 0,
        pre: tuple | None = None) -> SemVer:
    return SemVer(major, minor, patch, pre)


def _expand_comparator(token: str) -> list[tuple[str, SemVer]]:
    """把单个比较器 token 展开为 (op, 具体版本) 约束列表。"""
    token = token.strip()
    if token in ("", "*", "x", "X"):
        return []

    m = re.match(r"^(\^|~>|~|>=|<=|>|<|=)?\s*(.+)$", token)
    op, ver_text = m.group(1), m.group(2)
    p = _parse_partial(ver_text)

    # 全局通配
    if p.major is None:
        return []

    if op == "^":
        low = _sv(p.major, p.minor or 0, p.patch or 0, p.pre)
        if p.major > 0:
            high = _sv(p.major + 1, 0, 0)
        elif p.minor and p.minor > 0:
            high = _sv(0, p.minor + 1, 0)
        elif p.minor:  # ^0.0.x
            high = _sv(0, 0, (p.patch or 0) + 1)
        else:
            high = _sv(0, 0, (p.patch or 0) + 1)
        return [(">=", low), ("<", high)]

    if op in ("~", "~>"):
        if p.minor is None:
            return [(">=", _sv(p.major, 0, 0, p.pre)), ("<", _sv(p.major + 1, 0, 0))]
        if p.patch is None:
            return [(">=", _sv(p.major, p.minor, 0, p.pre)),
                    ("<", _sv(p.major, p.minor + 1, 0))]
        return [(">=", _sv(p.major, p.minor, p.patch, p.pre)),
                ("<", _sv(p.major, p.minor + 1, 0))]

    # 无显式操作符的 partial 版本 -> x 范围（精确到给定段）
    if op is None:
        if p.minor is None:
            return [(">=", _sv(p.major, 0, 0, p.pre)), ("<", _sv(p.major + 1, 0, 0))]
        if p.patch is None:
            return [(">=", _sv(p.major, p.minor, 0, p.pre)),
                    ("<", _sv(p.major, p.minor + 1, 0))]
        return [(">=", _sv(p.major, p.minor, p.patch, p.pre)),
                ("<=", _sv(p.major, p.minor, p.patch, p.pre))]

    low = _sv(p.major, p.minor or 0, p.patch or 0, p.pre)
    if op == ">=":
        return [(">=", low)]
    if op == "<=":
        if p.patch is None:
            if p.minor is None:
                return [("<", _sv(p.major + 1, 0, 0))]
            return [("<", _sv(p.major, p.minor + 1, 0))]
        return [("<=", low)]
    if op == ">":
        if p.patch is None:
            if p.minor is None:
                return [(">=", _sv(p.major + 1, 0, 0))]
            return [(">=", _sv(p.major, p.minor + 1, 0))]
        return [(">", low)]
    if op == "<":
        return [("<", low)]
    if op == "=":
        if p.minor is None:
            return [(">=", _sv(p.major, 0, 0)), ("<", _sv(p.major + 1, 0, 0))]
        if p.patch is None:
            return [(">=", _sv(p.major, p.minor, 0)),
                    ("<", _sv(p.major, p.minor + 1, 0))]
        return [("=", low)]
    raise VersionError(f"unsupported semver operator: {op}")  # pragma: no cover


def _expand_hyphen(set_text: str) -> str:
    """``1.2.3 - 2.3.4`` -> 比较器串。"""
    m = re.search(r"(\S+)\s+-\s+(\S+)", set_text)
    if not m:
        return set_text
    left, right = _parse_partial(m.group(1)), _parse_partial(m.group(2))
    if left.major is None:
        raise VersionError("hyphen range has empty lower bound")
    low = _sv(left.major, left.minor or 0, left.patch or 0, left.pre)
    if right.patch is not None:
        hi_text = f"<={right.major}.{right.minor}.{right.patch}"
    elif right.minor is not None:
        hi_text = f"<{right.major}.{right.minor + 1}.0"
    else:
        hi_text = f"<{right.major + 1}.0.0"
    expanded = f">={low.major}.{low.minor}.{low.patch}"
    if low.pre:
        expanded += "-" + ".".join(str(i) for i in low.pre)
    return set_text.replace(m.group(0), f"{expanded} {hi_text}")


def _pre_cores(set_text: str) -> set[tuple[int, int, int]]:
    """收集该范围集合内出现的预发布版本 (major,minor,patch)。"""
    cores: set[tuple[int, int, int]] = set()
    for tok in set_text.split():
        mt = re.match(r"^(?:\^|~>=?|>=|<=|>|<|=)?\s*(.+)$", tok)
        if not mt:
            continue
        m = _SEMVER_RE.match(mt.group(1))
        if m and m.group("pre"):
            cores.add((int(m.group("major")), int(m.group("minor")),
                       int(m.group("patch"))))
    return cores


def _check(op: str, ver: SemVer, bound: SemVer) -> bool:
    c = semver_compare(ver, bound)
    return {">=": c >= 0, "<=": c <= 0, ">": c > 0, "<": c < 0,
            "=": c == 0}[op]


def npm_satisfies(version: str, spec: str) -> bool:
    ver = SemVer.parse(version)
    for raw_set in spec.split("||"):
        set_text = _expand_hyphen(raw_set.strip())
        tokens = [t for t in set_text.split() if t]
        constraints: list[tuple[str, SemVer]] = []
        for tok in tokens:
            constraints.extend(_expand_comparator(tok))
        if all(_check(op, ver, bound) for op, bound in constraints):
            # npm 预发布门禁：预发布版本只有在集合中出现同一
            # (major,minor,patch) 的预发布比较器时才可能命中
            if ver.pre is not None and ver.core() not in _pre_cores(set_text):
                continue
            return True
    return False


# ---------------------------------------------------------------------------
# Maven
# ---------------------------------------------------------------------------

_MAVEN_TOKENS = re.compile(r"\d+|[a-zA-Z]+")
_MAVEN_QUALIFIERS = {
    "alpha": 1, "a": 1,
    "beta": 2, "b": 2,
    "milestone": 3, "m": 3,
    "rc": 4, "cr": 4,
    "snapshot": 5,
    "": 6, "ga": 6, "final": 6, "release": 6,
    "sp": 7,
}
_MAVEN_GROUP_RE = re.compile(r"([\[\(])\s*([^[\]()]*?)\s*([\]\)])")


@dataclass(frozen=True)
class MavenItem:
    numeric: bool
    value: int
    name: str = ""

    @staticmethod
    def num(n: int) -> "MavenItem":
        return MavenItem(True, n)

    @staticmethod
    def qual(name: str) -> "MavenItem":
        name = name.lower()
        if name in _MAVEN_QUALIFIERS:
            return MavenItem(False, _MAVEN_QUALIFIERS[name], name)
        return MavenItem(False, _MAVEN_QUALIFIERS["sp"] + 1, name)


_RELEASE_ITEM = MavenItem.qual("")


def _maven_items(text: str) -> list[MavenItem]:
    items: list[MavenItem] = []
    for tok in _MAVEN_TOKENS.findall(text.lower()):
        if tok.isdigit():
            items.append(MavenItem.num(int(tok)))
        else:
            items.append(MavenItem.qual(tok))
    return items


def _maven_pad(other: MavenItem) -> MavenItem:
    """缺位填充：对位是数字则补 0；对位是限定词则补 release(``""``)。"""
    return MavenItem.num(0) if other.numeric else _RELEASE_ITEM


def _maven_cmp_item(a: MavenItem, b: MavenItem) -> int:
    if a.numeric and b.numeric:
        return (a.value > b.value) - (a.value < b.value)
    if a.numeric:
        return 1  # 数字段高于相邻限定词
    if b.numeric:
        return -1
    if a.value != b.value:
        return -1 if a.value < b.value else 1
    return (a.name > b.name) - (a.name < b.name)


def maven_compare(a: str, b: str) -> int:
    ia, ib = _maven_items(a), _maven_items(b)
    n = max(len(ia), len(ib))
    for i in range(n):
        x = ia[i] if i < len(ia) else _maven_pad(ib[i])
        y = ib[i] if i < len(ib) else _maven_pad(ia[i])
        c = _maven_cmp_item(x, y)
        if c:
            return c
    return 0


def maven_satisfies(version: str, spec: str) -> bool:
    spec = spec.strip()
    if not spec:
        raise VersionError("empty maven range")
    # 提前确保版本可解析（无 token = 非法）
    if not _maven_items(version):
        raise VersionError(f"unparseable maven version: {version!r}")

    groups = _MAVEN_GROUP_RE.findall(spec)
    if not groups:
        # 软要求/普通版本：按精确版本处理
        if not _maven_items(spec):
            raise VersionError(f"unparseable maven range: {spec!r}")
        return maven_compare(version, spec) == 0

    for lb, content, rb in groups:
        parts = [p.strip() for p in content.split(",")]
        if len(parts) == 1 and lb == "[" and rb == "]":
            if maven_compare(version, parts[0]) != 0:
                continue
            return True
        low = parts[0] if parts[0] else None
        high = parts[1] if len(parts) > 1 and parts[1] else None
        ok = True
        if low is not None:
            c = maven_compare(version, low)
            ok = c >= 0 if lb == "[" else c > 0
        if ok and high is not None:
            c = maven_compare(version, high)
            ok = c <= 0 if rb == "]" else c < 0
        if ok:
            return True
    return False


# ---------------------------------------------------------------------------
# PyPI / PEP 440（packaging 的真实实现）
# ---------------------------------------------------------------------------

def pypi_satisfies(version: str, spec: str) -> bool:
    try:
        ver = Version(version)
        spec_set = SpecifierSet(spec)
    except InvalidVersion as exc:
        raise VersionError(f"invalid PEP 440 version: {version!r}") from exc
    except ValueError as exc:
        raise VersionError(f"invalid PEP 440 specifier set: {spec!r}") from exc
    # prereleases=True：让 rc/a/dev 等预发布版本按真实数值顺序参与比较，
    # 但显式排除约束（如 <1.5）仍然生效。
    return spec_set.contains(ver, prereleases=True)


# ---------------------------------------------------------------------------
# RubyGems
# ---------------------------------------------------------------------------

_GEM_TOKENS = re.compile(r"\d+|[a-z]+")


def _gem_items(text: str) -> list[int | str]:
    items: list[int | str] = []
    for tok in _GEM_TOKENS.findall(text.lower()):
        items.append(int(tok) if tok.isdigit() else tok)
    # RubyGems 版本必须至少含一个数字段
    if not items or not any(isinstance(i, int) for i in items):
        raise VersionError(f"unparseable gem version: {text!r}")
    return items


def _gem_cmp(a: str, b: str) -> int:
    ia, ib = _gem_items(a), _gem_items(b)
    n = max(len(ia), len(ib))
    for i in range(n):
        x: int | str = ia[i] if i < len(ia) else 0
        y: int | str = ib[i] if i < len(ib) else 0
        if isinstance(x, int) and isinstance(y, int):
            if x != y:
                return -1 if x < y else 1
        elif isinstance(x, str) and isinstance(y, str):
            if x != y:
                return -1 if x < y else 1
        elif isinstance(x, str):
            return -1  # 字母段（预发布）低于数字段
        else:
            return 1
    return 0


_GEM_OP_RE = re.compile(r"(~>|>=|<=|!=|>|<|=)\s*([0-9][0-9a-zA-Z.\-]*)")


def gem_satisfies(version: str, spec: str) -> bool:
    _gem_items(version)  # 校验
    clauses = [c.strip() for c in spec.split(",") if c.strip()]
    if not clauses:
        raise VersionError("empty gem requirement")
    for clause in clauses:
        m = _GEM_OP_RE.match(clause)
        if not m:
            raise VersionError(f"unparseable gem requirement: {clause!r}")
        op, target = m.group(1), m.group(2)
        c = _gem_cmp(version, target)
        if op == "=":
            if c != 0:
                return False
        elif op == "!=":
            if c == 0:
                return False
        elif op == ">" and not c > 0:
            return False
        elif op == "<" and not c < 0:
            return False
        elif op == ">=" and not c >= 0:
            return False
        elif op == "<=" and not c <= 0:
            return False
        elif op == "~>":
            if c < 0:
                return False
            # 上界：取最后一个数字段所在前缀，将其前一段 +1 并补零。
            # ~> 1.2.3 -> <1.3.0；~> 1.2 -> <2.0；~> 1 -> <2.0
            nums = [int(t) for t in _GEM_TOKENS.findall(target.lower())
                    if t.isdigit()]
            dotted = target.split(".")
            pre_numeric = 0
            for seg in dotted:
                if seg.isdigit():
                    pre_numeric += 1
                else:
                    break
            if pre_numeric < 2:
                upper = f"{nums[0] + 1}.0"
            else:
                prefix = nums[: pre_numeric - 1]
                prefix[-1] += 1
                upper = ".".join(str(n) for n in prefix + [0])
            if _gem_cmp(version, upper) >= 0:
                return False
    return True


# ---------------------------------------------------------------------------
# dpkg / Debian
# ---------------------------------------------------------------------------

def _dpkg_order(ch: str) -> int:
    """dpkg order()：数字与串尾均为 0，'~' 为 -1，字母取其 ASCII，
    其它字符取 ASCII+256（排在字母之后）。"""
    if ch == "" or ch.isdigit():
        return 0
    if ch == "~":
        return -1
    if ch.isalpha():
        return ord(ch)
    return ord(ch) + 256


def _dpkg_fragment_cmp(a: str, b: str) -> int:
    i = j = 0
    while i < len(a) or j < len(b):
        # 非数字段
        while (i < len(a) and not a[i].isdigit()) or \
              (j < len(b) and not b[j].isdigit()):
            ac = _dpkg_order(a[i] if i < len(a) else "")
            bc = _dpkg_order(b[j] if j < len(b) else "")
            if ac != bc:
                return -1 if ac < bc else 1
            if i < len(a):
                i += 1
            if j < len(b):
                j += 1
        # 数字段
        an = bn = 0
        while i < len(a) and a[i].isdigit():
            an = an * 10 + int(a[i]); i += 1
        while j < len(b) and b[j].isdigit():
            bn = bn * 10 + int(b[j]); j += 1
        if an != bn:
            return -1 if an < bn else 1
    return 0


@dataclass(frozen=True)
class DebVersion:
    epoch: int
    upstream: str
    revision: str


def _parse_deb(text: str) -> DebVersion:
    if not text or not re.match(r"^[0-9A-Za-z][0-9A-Za-z.+~:+-]*$", text):
        raise VersionError(f"unparseable dpkg version: {text!r}")
    body = text
    epoch = 0
    if ":" in body:
        ep, body = body.split(":", 1)
        if not ep.isdigit():
            raise VersionError(f"invalid epoch in dpkg version: {text!r}")
        epoch = int(ep)
    if "-" in body:
        upstream, revision = body.rsplit("-", 1)
    else:
        upstream, revision = body, "0"
    return DebVersion(epoch, upstream, revision)


def dpkg_compare(a: str, b: str) -> int:
    da, db = _parse_deb(a), _parse_deb(b)
    if da.epoch != db.epoch:
        return -1 if da.epoch < db.epoch else 1
    c = _dpkg_fragment_cmp(da.upstream, db.upstream)
    if c:
        return c
    return _dpkg_fragment_cmp(da.revision, db.revision)


_DEB_OP_RE = re.compile(r"(>=|<=|>|<|=)\s*(\S+)")


def deb_satisfies(version: str, spec: str) -> bool:
    _parse_deb(version)
    clauses = [c.strip() for c in spec.split(",") if c.strip()]
    if not clauses:
        raise VersionError("empty deb version range")
    for clause in clauses:
        m = _DEB_OP_RE.match(clause)
        if not m:
            raise VersionError(f"unparseable deb comparator: {clause!r}")
        op, target = m.group(1), m.group(2)
        c = dpkg_compare(version, target)
        checks = {">=": c >= 0, "<=": c <= 0, ">": c > 0,
                  "<": c < 0, "=": c == 0}
        if not checks[op]:
            return False
    return True


# ---------------------------------------------------------------------------
# 统一入口
# ---------------------------------------------------------------------------

_COMPARATORS = {
    "npm": npm_satisfies,
    "maven": maven_satisfies,
    "pypi": pypi_satisfies,
    "gem": gem_satisfies,
    "deb": deb_satisfies,
}

#: 具备真实版本比较器的生态集合
SUPPORTED_ECOSYSTEMS = set(_COMPARATORS)


def evaluate(ecosystem: str, version: str | None, spec: str,
             range_type: str) -> tuple[bool, str | None]:
    """评估版本是否落在范围内。

    返回 ``(affected, reason)``：正常命中/未命中 reason 为 ``None``；
    版本缺失或无法解析时 affected=False、reason 给出未知原因。
    """
    if version is None or str(version).strip() == "":
        return False, "missing_version"
    if ecosystem not in _COMPARATORS:
        return False, "unsupported_ecosystem"
    try:
        return bool(_COMPARATORS[ecosystem](str(version), spec)), None
    except (VersionError, UnsupportedEcosystemError):
        return False, "invalid_version"
