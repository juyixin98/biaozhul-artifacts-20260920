"""自定义 SemVer 区间语法。

语法（有意做成受限、无歧义的子集）：

- 单个比较器：``OP VERSION``，OP ∈ ``=`` ``!=`` ``>`` ``>=`` ``<`` ``<=``；
  ``=`` 可省略，即 ``1.2.3`` 等价于 ``=1.2.3``。
- 区间（AND 交集）：比较器之间用空白、逗号或 ``&&`` 连接，全部满足才命中。
  例如 ``>=1.0.0 <2.0.0``、``^1.2.3``、``~1.4.0, >=1.4.2``。
- ``^`` 脱字符：锁定主版本（0.x 锁定次版本，0.0.x 锁定修订），
  与 npm 语义一致：``^1.2.3 := >=1.2.3 <2.0.0``，
  ``^0.2.3 := >=0.2.3 <0.3.0``，``^0.0.3 := >=0.0.3 <0.0.4``。
- ``~`` 波浪号：锁定主.次版本：``~1.2.3 := >=1.2.3 <1.3.0``，
  ``~1.2 := =1.2.x`` 这类缩写 **不支持**（必须给全三段）。
- ``*``：通配，匹配任意稳定核心的版本。
- 空白整体忽略；空区间等价于 ``*``。
- 不支持 OR（``||``）、x-range（``1.2.x``）、连字符区间（``1.0 - 2.0``）。
  一条漏洞若需要多个不相交区间，拆成多条漏洞记录。

预发布策略（安全优先，明确文档化）：

1. 候选版本为稳定版时，只按数值比较，比较器里的预发布段按普通版本优先级参与。
2. 候选为预发布版时，必须**至少有一个比较器**通过“预发布门”，
   即该比较器的参照版本与候选共享同一 major.minor.patch 核心，
   且属于以下之一：
   a) 是精确相等（``=``/裸版本）比较器；
   b) 是由 ``^``/``~`` 展开而来的区间边界（^/~ 的下界自然与候选同核心）；
   c) 是 ``>=``/``>``/``<=``/``<`` 且其参照版本本身带预发布段并与候选同核心。
   此外，候选仍必须通过全部比较器的数值比较。
3. ``*`` 永不匹配预发布版本（避免把通配范围泄漏到尚未稳定的版本）。
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import List, Optional

from .semver import Version, parse_version, VersionParseError

OPS = ("=", "!=", ">=", "<=", ">", "<")


class RangeParseError(ValueError):
    """区间字符串语法错误。"""


# 从规范化后的字符串中逐个取 token：操作符（可省略）+ 完整 SemVer 或 *
_TOKEN_RE = re.compile(
    r"(?P<op>>=|<=|!=|=|>|<|\^|~)?(?P<ver>\d+\.\d+\.\d+(?:-[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?|\*)"
)
# 合法分隔符：空白、逗号、&&
_SEP_RE = re.compile(r"(\s|&&|,)+")


@dataclass(frozen=True)
class Comparator:
    op: str  # '=', '!=', '>', '>=', '<', '<=', '*'
    version: Optional[Version]
    from_hyphen: bool = False  # 是否由 ^ / ~ 展开产生（预发布门 b）

    def matches(self, candidate: Version) -> bool:
        if self.op == "*":
            return True
        v = self.version
        if self.op == "=":
            return candidate == v
        if self.op == "!=":
            return candidate != v
        if self.op == ">":
            return candidate > v
        if self.op == ">=":
            return candidate >= v
        if self.op == "<":
            return candidate < v
        if self.op == "<=":
            return candidate <= v
        raise RangeParseError(f"未知操作符: {self.op!r}")

    def admits_prerelease(self, candidate: Version) -> bool:
        """预发布门：该比较器是否允许同核心的预发布候选。"""
        if self.op == "*":
            return False
        if not candidate.is_prerelease:
            return True
        if candidate.core != self.version.core:
            return False
        if self.op == "=":
            return True
        if self.from_hyphen:
            return True
        if self.op in (">", ">=", "<", "<=") and self.version.is_prerelease:
            return True
        return False


@dataclass(frozen=True)
class VersionRange:
    raw: str
    comparators: List[Comparator]

    def satisfies(self, candidate: Version) -> bool:
        if not self.comparators:
            return not candidate.is_prerelease
        if not all(c.matches(candidate) for c in self.comparators):
            return False
        if candidate.is_prerelease:
            return any(c.admits_prerelease(candidate) for c in self.comparators)
        return True


def _caret_bounds(v: Version) -> List[Comparator]:
    """^ 脱字符展开为 [>=v, <upper)。"""
    if v.major > 0:
        upper = Version(v.major + 1, 0, 0)
    elif v.minor > 0:
        upper = Version(0, v.minor + 1, 0)
    else:
        upper = Version(0, 0, v.patch + 1)
    return [
        Comparator(">=", v, from_hyphen=True),
        Comparator("<", upper, from_hyphen=True),
    ]


def _tilde_bounds(v: Version) -> List[Comparator]:
    """~ 波浪号展开为 [>=v, <major.(minor+1).0)。"""
    upper = Version(v.major, v.minor + 1, 0)
    return [
        Comparator(">=", v, from_hyphen=True),
        Comparator("<", upper, from_hyphen=True),
    ]


def parse_range(text: str) -> VersionRange:
    """解析区间字符串，失败抛 :class:`RangeParseError`。"""
    if text is None:
        raise RangeParseError("区间为 null")
    if not isinstance(text, str):
        range_str = str(text)
    else:
        range_str = text

    stripped = range_str.strip()
    if stripped == "" or stripped == "*":
        return VersionRange(raw=range_str, comparators=[] if stripped == "" else [Comparator("*", None)])

    # 校验：所有字符必须能被 token 或分隔符消费，否则说明有非法语法（x-range、|| 等）
    consumed = []
    comparators: List[Comparator] = []
    pos = 0
    saw_wildcard = False
    while pos < len(stripped):
        m_sep = _SEP_RE.match(stripped, pos)
        if m_sep:
            consumed.append(m_sep.group(0))
            pos = m_sep.end()
            continue
        m = _TOKEN_RE.match(stripped, pos)
        if not m:
            raise RangeParseError(f"无法解析的区间片段: {stripped[pos:]!r} (原始: {range_str!r})")
        consumed.append(m.group(0))
        pos = m.end()

        op = m.group("op")
        ver_text = m.group("ver")

        if ver_text == "*":
            if op is not None:
                raise RangeParseError(f"通配符 * 不能与操作符 {op} 组合 (原始: {range_str!r})")
            saw_wildcard = True
            comparators.append(Comparator("*", None))
            continue

        try:
            version = parse_version(ver_text)
        except VersionParseError as exc:
            raise RangeParseError(str(exc)) from exc

        if op == "^":
            comparators.extend(_caret_bounds(version))
        elif op == "~":
            comparators.extend(_tilde_bounds(version))
        elif op is None:
            comparators.append(Comparator("=", version))
        elif op in OPS:
            comparators.append(Comparator(op, version))
        else:  # pragma: no cover - 正则已限定
            raise RangeParseError(f"不支持的操作符 {op!r}")

    if saw_wildcard and len(comparators) != 1:
        raise RangeParseError(f"通配符 * 不能与其他约束组合 (原始: {range_str!r})")
    if not comparators:
        raise RangeParseError(f"空区间需写作 * 或留空，得到 {range_str!r}")
    return VersionRange(raw=range_str, comparators=comparators)


def satisfies(version: Version, range_text: str) -> bool:
    """便捷函数：解析区间并判断版本是否满足。"""
    return parse_range(range_text).satisfies(version)
