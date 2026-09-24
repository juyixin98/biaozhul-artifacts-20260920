"""有界的 SemVer 2.0.0 版本与范围语法实现。

支持的版本格式（组件版本必须完整）：
    MAJOR.MINOR.PATCH[-prerelease][+build]   例如 1.4.2、2.0.0-rc.1、1.0.0+build.5

支持的范围语法（npm 风格的一个子集，刻意保持有界）：
    1.2.3            精确匹配（等价于 =1.2.3）
    =1.2.3           精确匹配
    > >= < <= 1.2.3  比较符，可配合部分版本（如 >=1.2 表示 >=1.2.0）
    1.2  1           部分版本，等价于通配（1.2.x / 1.x.x）
    1.2.x  1.x  *    通配符
    ^1.2.3           兼容范围：>=1.2.3 <2.0.0（0.x 按 semver 规则收紧）
    ~1.2.3           近似范围：>=1.2.3 <1.3.0
    >=1.0.0 <2.0.0   空格或逗号分隔的比较符交集（AND）
    a || b           并集（OR）

预发布版本规则（与 npm semver 一致）：
    仅当同一比较符集合中至少有一个比较符带有相同 [major, minor, patch]
    且含预发布标识时，预发布版本才能命中该集合。例如
    ">=2.0.0-alpha <2.0.0" 能命中 2.0.0-rc.1，而 "<2.0.0" 不能。

不支持（解析时报 RangeSyntaxError）：连字符范围 "1.2.3 - 2.0.0"。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from functools import total_ordering

__all__ = ["Version", "Range", "parse_version", "parse_range", "RangeSyntaxError"]


class RangeSyntaxError(ValueError):
    """范围语法无法解析。"""


_VERSION_RE = re.compile(
    r"^v?\s*"
    r"(0|[1-9]\d*)"                       # major
    r"(?:\.(0|[1-9]\d*|x|\*))?"           # minor（可省略或为通配符）
    r"(?:\.(0|[1-9]\d*|x|\*))?"           # patch（可省略或为通配符）
    r"(?:-([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?"   # prerelease
    r"(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?"    # build（比较时忽略）
    r"\s*$"
)


def _parse_prerelease(text: str | None) -> tuple:
    if not text:
        return ()
    identifiers = []
    for ident in text.split("."):
        if ident.isdigit():
            identifiers.append(int(ident))
        else:
            identifiers.append(ident)
    return tuple(identifiers)


@total_ordering
@dataclass(frozen=True)
class Version:
    """一个完整的 SemVer 版本。build 元数据不参与比较。"""

    major: int
    minor: int
    patch: int
    prerelease: tuple = field(default_factory=tuple, compare=False)

    def _key(self):
        return (self.major, self.minor, self.patch)

    def __str__(self) -> str:
        base = f"{self.major}.{self.minor}.{self.patch}"
        if self.prerelease:
            base += "-" + ".".join(str(p) for p in self.prerelease)
        return base

    def __eq__(self, other) -> bool:
        if not isinstance(other, Version):
            return NotImplemented
        return self._key() == other._key() and self._pre_key() == other._pre_key()

    def __hash__(self) -> int:
        return hash((self._key(), self._pre_key()))

    def _pre_key(self):
        # 将预发布标识转为可比较的元组：数字标识 < 字母标识；
        # 无预发布 > 有预发布，用前置标志位表达。
        if not self.prerelease:
            return (1,)
        key = [0]
        for ident in self.prerelease:
            if isinstance(ident, int):
                key.append((0, ident, ""))
            else:
                key.append((1, 0, ident))
        return tuple(key)

    def __lt__(self, other) -> bool:
        if not isinstance(other, Version):
            return NotImplemented
        if self._key() != other._key():
            return self._key() < other._key()
        # 预发布比较：逐标识比较，数字 < 字母；公共前缀相同则短者小。
        a, b = self.prerelease, other.prerelease
        if not a and not b:
            return False
        if not a:
            return False  # 正式版 > 任何预发布
        if not b:
            return True
        for x, y in zip(a, b):
            if x == y:
                continue
            x_num, y_num = isinstance(x, int), isinstance(y, int)
            if x_num and y_num:
                return x < y
            if x_num:
                return True  # 数字标识 < 字母标识
            if y_num:
                return False
            return x < y
        return len(a) < len(b)


def parse_version(text: str) -> Version:
    """解析具体版本号，必须是完整的 X.Y.Z（范围语法中的部分版本由 _parse_partial 处理）。"""
    m = _VERSION_RE.match(text.strip())
    if not m:
        raise ValueError(f"不是合法的 SemVer 版本: {text!r}")
    major, minor, patch, pre = m.groups()
    if minor in (None, "x", "*") or patch in (None, "x", "*"):
        raise ValueError(f"具体版本必须是完整的 X.Y.Z 形式: {text!r}")
    return Version(int(major), int(minor), int(patch), _parse_prerelease(pre))


def is_full_version(text: str) -> bool:
    """判断是否为完整的 X.Y.Z 版本（组件版本必须完整）。"""
    m = _VERSION_RE.match(text.strip())
    if not m:
        return False
    _, minor, patch, _ = m.groups()
    return minor not in (None, "x", "*") and patch not in (None, "x", "*")


@dataclass(frozen=True)
class _Comparator:
    """一个比较符，归一化为对 Version 的有序测试 + 预发布门控信息。"""

    op: str  # one of: lt, lte, gt, gte, eq
    version: Version
    gate_triple: tuple | None = None  # (major, minor, patch)
    gate_has_prerelease: bool = False

    def test(self, v: Version) -> bool:
        if self.op == "lt":
            return v < self.version
        if self.op == "lte":
            return v <= self.version
        if self.op == "gt":
            return v > self.version
        if self.op == "gte":
            return v >= self.version
        return v == self.version


def _bump(v: Version, level: str) -> Version:
    if level == "major":
        return Version(v.major + 1, 0, 0)
    if level == "minor":
        return Version(v.major, v.minor + 1, 0)
    return Version(v.major, v.minor, v.patch + 1)


_COMPARATOR_RE = re.compile(r"^(>=|<=|>|<|=)?\s*(.+)$")


def _parse_partial(text: str):
    """解析（可能部分的）版本串，返回 (major, minor, patch, prerelease, missing_level)。

    missing_level: None 表示完整；'minor' 表示只有 major；'patch' 表示缺 patch。
    """
    m = _VERSION_RE.match(text.strip())
    if not m:
        raise RangeSyntaxError(f"无法解析版本: {text!r}")
    major, minor, patch, pre = m.groups()
    pre_tuple = _parse_prerelease(pre)
    if minor is None or minor in ("x", "*"):
        return int(major), None, None, pre_tuple, "minor"
    if patch is None or patch in ("x", "*"):
        return int(major), int(minor), None, pre_tuple, "patch"
    return int(major), int(minor), int(patch), pre_tuple, None


def _simple_comparators(op: str, text: str) -> list[_Comparator]:
    major, minor, patch, pre, missing = _parse_partial(text)
    gate_triple = None
    if missing is None:
        gate_triple = (major, minor, patch)
    base = Version(major, minor or 0, patch or 0, pre)
    has_pre = bool(pre)

    def comp(op_: str, v: Version) -> _Comparator:
        return _Comparator(op_, v, gate_triple, has_pre)

    if op in ("", "="):
        if missing == "minor":
            return [comp("gte", Version(major, 0, 0)), comp("lt", Version(major + 1, 0, 0))]
        if missing == "patch":
            return [comp("gte", Version(major, minor, 0)), comp("lt", Version(major, minor + 1, 0))]
        return [comp("eq", base)]
    if op == ">":
        if missing == "minor":
            return [comp("gte", Version(major + 1, 0, 0))]
        if missing == "patch":
            return [comp("gte", Version(major, minor + 1, 0))]
        return [comp("gt", base)]
    if op == ">=":
        return [comp("gte", Version(major, minor or 0, patch or 0, pre))]
    if op == "<":
        return [comp("lt", Version(major, minor or 0, patch or 0, pre))]
    if op == "<=":
        if missing == "minor":
            return [comp("lt", Version(major + 1, 0, 0))]
        if missing == "patch":
            return [comp("lt", Version(major, minor + 1, 0))]
        return [comp("lte", base)]
    raise RangeSyntaxError(f"不支持的比较符: {op!r}")


def _caret_comparators(text: str) -> list[_Comparator]:
    major, minor, patch, pre, missing = _parse_partial(text)
    base = Version(major, minor or 0, patch or 0, pre)
    lower = _Comparator("gte", base,
                        (major, minor, patch) if missing is None else None, bool(pre))
    if missing == "minor":
        upper_v = Version(major + 1, 0, 0)
    elif missing == "patch":
        upper_v = Version(major + 1, 0, 0)
    elif major > 0:
        upper_v = Version(major + 1, 0, 0)
    elif minor > 0:
        upper_v = Version(0, minor + 1, 0)
    else:
        upper_v = Version(0, 0, patch + 1)
    upper = _Comparator("lt", upper_v, lower.gate_triple, bool(pre))
    return [lower, upper]


def _tilde_comparators(text: str) -> list[_Comparator]:
    major, minor, patch, pre, missing = _parse_partial(text)
    base = Version(major, minor or 0, patch or 0, pre)
    lower = _Comparator("gte", base,
                        (major, minor, patch) if missing is None else None, bool(pre))
    if missing == "minor":
        upper_v = Version(major + 1, 0, 0)
    else:
        upper_v = Version(major, (minor or 0) + 1, 0)
    upper = _Comparator("lt", upper_v, lower.gate_triple, bool(pre))
    return [lower, upper]


@dataclass(frozen=True)
class _ComparatorSet:
    comparators: tuple

    def matches(self, v: Version) -> bool:
        if not all(c.test(v) for c in self.comparators):
            return False
        if not v.prerelease:
            return True
        # 预发布门控：需要同集合内存在同 [major,minor,patch] 且带预发布的比较符
        triple = (v.major, v.minor, v.patch)
        return any(
            c.gate_has_prerelease and c.gate_triple == triple for c in self.comparators
        )


class Range:
    """一个范围表达式：多个比较符集合的并集。"""

    def __init__(self, sets: list[_ComparatorSet], source: str):
        self._sets = sets
        self.source = source

    def matches(self, v: Version) -> bool:
        return any(s.matches(v) for s in self._sets)

    def __repr__(self) -> str:
        return f"Range({self.source!r})"


def _parse_set(text: str) -> _ComparatorSet:
    text = text.strip()
    if not text or text == "*":
        return _ComparatorSet(())
    if " - " in text:
        raise RangeSyntaxError("不支持连字符范围 'a - b'，请改用 '>=a <=b'")
    comparators: list[_Comparator] = []
    tokens = [t for t in re.split(r"[\s,]+", text) if t]
    for token in tokens:
        if token.startswith("^"):
            comparators.extend(_caret_comparators(token[1:]))
        elif token.startswith("~"):
            comparators.extend(_tilde_comparators(token[1:]))
        else:
            m = _COMPARATOR_RE.match(token)
            if not m:
                raise RangeSyntaxError(f"无法解析比较符: {token!r}")
            op, ver = m.groups()
            comparators.extend(_simple_comparators(op or "", ver))
    return _ComparatorSet(tuple(comparators))


def parse_range(text: str) -> Range:
    """解析范围表达式；语法错误抛 RangeSyntaxError。"""
    if not isinstance(text, str) or not text.strip():
        raise RangeSyntaxError("范围表达式不能为空")
    parts = text.split("||")
    sets = [_parse_set(part) for part in parts]
    return Range(sets, text)
