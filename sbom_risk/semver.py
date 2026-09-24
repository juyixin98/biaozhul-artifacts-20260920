"""Semantic Versioning 2.0.0 解析与优先级比较。

严格遵循 https://semver.org/spec/v2.0.0.html ：
- 主.次.修订 三段必须为数字，前导零非法（0 本身合法）；
- 预发布标识以 ``-`` 引入，点号分段；数字标识按数值比较且小于字母标识；
- 任一预发布版本 < 同核心的稳定版本；
- 构建元数据（``+build``）合法但不参与优先级比较。
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from functools import total_ordering
from typing import Tuple, Union

# 官方 SemVer 2.0 正则（命名分组便于阅读）
_SEMVER_RE = re.compile(
    r"^(?P<major>0|[1-9]\d*)"
    r"\.(?P<minor>0|[1-9]\d*)"
    r"\.(?P<patch>0|[1-9]\d*)"
    r"(?:-(?P<pre>[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?"
    r"(?:\+(?P<build>[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?$"
)

_PRE_NUMERIC_RE = re.compile(r"^(0|[1-9]\d*)$")

PreId = Union[int, str]


class VersionParseError(ValueError):
    """字符串不符合 SemVer 2.0.0。"""


@total_ordering
@dataclass(frozen=True)
class Version:
    major: int
    minor: int
    patch: int
    prerelease: Tuple[PreId, ...] = ()
    build: str | None = None

    @property
    def is_prerelease(self) -> bool:
        return bool(self.prerelease)

    @property
    def core(self) -> Tuple[int, int, int]:
        return (self.major, self.minor, self.patch)

    def _precedence_key(self) -> tuple:
        # 无预发布段的版本更大：稳定标记位 1，预发布为 0。
        if self.prerelease:
            ids = tuple((0, i) if isinstance(i, int) else (1, i) for i in self.prerelease)
            pre_key = (0, ids)
        else:
            pre_key = (1, ())
        return (self.major, self.minor, self.patch, pre_key)

    def __lt__(self, other: "Version") -> bool:
        if not isinstance(other, Version):
            return NotImplemented
        return self._precedence_key() < other._precedence_key()

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, Version):
            return NotImplemented
        # 构建元数据不参与比较
        return self._precedence_key() == other._precedence_key()

    def __hash__(self) -> int:
        return hash(self._precedence_key())

    def __str__(self) -> str:
        s = f"{self.major}.{self.minor}.{self.patch}"
        if self.prerelease:
            s += "-" + ".".join(str(i) for i in self.prerelease)
        if self.build is not None:
            s += "+" + self.build
        return s


def _valid_identifiers(segment: str) -> bool:
    """预发布/构建元数据段：数字标识符禁止前导零（构建段的字母数字不受此限）。"""
    for ident in segment.split("."):
        if ident.isdigit() and len(ident) > 1 and ident[0] == "0":
            return False
    return True


def parse_version(text: str) -> Version:
    """把字符串解析为 :class:`Version`，失败抛 :class:`VersionParseError`。"""
    if not isinstance(text, str):
        raise VersionParseError(f"版本必须是字符串，得到 {type(text).__name__}")
    m = _SEMVER_RE.match(text.strip())
    if not m:
        raise VersionParseError(f"不符合 SemVer 2.0.0: {text!r}")

    pre = m.group("pre")
    if pre is not None and not _valid_identifiers(pre):
        raise VersionParseError(f"数字预发布标识符不允许前导零: {text!r}")

    pre_ids: Tuple[PreId, ...] = ()
    if pre is not None:
        pre_ids = tuple(
            int(seg) if _PRE_NUMERIC_RE.match(seg) else seg
            for seg in pre.split(".")
        )
    return Version(
        major=int(m.group("major")),
        minor=int(m.group("minor")),
        patch=int(m.group("patch")),
        prerelease=pre_ids,
        build=m.group("build"),
    )
