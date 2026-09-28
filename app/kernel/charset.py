"""字符集：固定 Unicode 字母表上的有序不相交区间集合。

区间均为闭区间码点 [lo, hi]。所有操作返回新对象（不可变模式）。
"""

from __future__ import annotations

from dataclasses import dataclass

from app.config import ALPHABET_HI, ALPHABET_LO

Interval = tuple[int, int]


@dataclass(frozen=True)
class CharSet:
    intervals: tuple[Interval, ...]  # 排序、不相交、不相邻

    # ---- 构造 ----
    @staticmethod
    def empty() -> "CharSet":
        return CharSet(())

    @staticmethod
    def full() -> "CharSet":
        return CharSet(((ALPHABET_LO, ALPHABET_HI),))

    @staticmethod
    def of_char(cp: int) -> "CharSet":
        return CharSet(((cp, cp),))

    @staticmethod
    def of_range(lo: int, hi: int) -> "CharSet":
        if lo > hi:
            raise ValueError(f"空区间: [{lo}, {hi}]")
        return CharSet(((lo, hi),))

    @staticmethod
    def normalize(intervals: list[Interval]) -> "CharSet":
        """把任意区间列表规范化为排序不相交形式。"""
        if not intervals:
            return CharSet.empty()
        ordered = sorted(intervals)
        merged: list[list[int]] = [[ordered[0][0], ordered[0][1]]]
        for lo, hi in ordered[1:]:
            last = merged[-1]
            if lo <= last[1] + 1:
                last[1] = max(last[1], hi)
            else:
                merged.append([lo, hi])
        return CharSet(tuple((lo, hi) for lo, hi in merged))

    # ---- 谓词 ----
    def is_empty(self) -> bool:
        return not self.intervals

    def contains(self, cp: int) -> bool:
        for lo, hi in self.intervals:
            if cp < lo:
                return False
            if lo <= cp <= hi:
                return True
        return False

    def min_char(self) -> int:
        """集合中最小码点；空集抛 ValueError。"""
        if not self.intervals:
            raise ValueError("空字符集无最小字符")
        return self.intervals[0][0]

    # ---- 运算（均返回新对象）----
    def union(self, other: "CharSet") -> "CharSet":
        return CharSet.normalize(list(self.intervals) + list(other.intervals))

    def intersect(self, other: "CharSet") -> "CharSet":
        out: list[Interval] = []
        i = j = 0
        a, b = self.intervals, other.intervals
        while i < len(a) and j < len(b):
            lo = max(a[i][0], b[j][0])
            hi = min(a[i][1], b[j][1])
            if lo <= hi:
                out.append((lo, hi))
            if a[i][1] < b[j][1]:
                i += 1
            else:
                j += 1
        return CharSet(tuple(out))

    def complement(self) -> "CharSet":
        out: list[Interval] = []
        nxt = ALPHABET_LO
        for lo, hi in self.intervals:
            if nxt < lo:
                out.append((nxt, lo - 1))
            nxt = max(nxt, hi + 1)
        if nxt <= ALPHABET_HI:
            out.append((nxt, ALPHABET_HI))
        return CharSet(tuple(out))

    def subtract(self, other: "CharSet") -> "CharSet":
        return self.intersect(other.complement())

    # ---- 展示 ----
    def describe(self) -> str:
        parts = []
        for lo, hi in self.intervals:
            if lo == hi:
                parts.append(f"U+{lo:04X}")
            else:
                parts.append(f"U+{lo:04X}-U+{hi:04X}")
        return "{" + ",".join(parts) + "}"


def partition_alphabet(charsets: list[CharSet]) -> list[CharSet]:
    """把所有字符集的区间端点切分成全字母表的不相交划分单元。

    任一输入字符集都是若干单元的并，用于子集构造时按单元转移。
    """
    boundaries = {ALPHABET_LO, ALPHABET_HI + 1}
    for cs in charsets:
        for lo, hi in cs.intervals:
            boundaries.add(lo)
            if hi + 1 <= ALPHABET_HI:
                boundaries.add(hi + 1)
    cuts = sorted(boundaries)
    cells: list[CharSet] = []
    for a, b in zip(cuts, cuts[1:]):
        cells.append(CharSet.of_range(a, b - 1))
    return cells
