"""区间语法与边界测试（含预发布门策略）。"""
from __future__ import annotations

import pytest

from sbom_risk.ranges import RangeParseError, parse_range, satisfies
from sbom_risk.semver import parse_version


def yes(version: str, range_text: str):
    assert satisfies(parse_version(version), range_text) is True, (
        f"{version!r} 应当满足 {range_text!r}"
    )


def no(version: str, range_text: str):
    assert satisfies(parse_version(version), range_text) is False, (
        f"{version!r} 不应满足 {range_text!r}"
    )


class TestBasicComparators:
    def test_inclusive_range_boundaries(self):
        r = ">=1.0.0 <2.0.0"
        yes("1.0.0", r)          # 下界含
        yes("1.9.9", r)
        no("0.9.9", r)
        no("2.0.0", r)           # 上界不含
        no("2.0.1", r)

    def test_closed_interval_boundaries(self):
        r = ">=3.2.0 <=3.2.1"
        yes("3.2.0", r)
        yes("3.2.1", r)          # 闭区间上界含
        no("3.1.9", r)
        no("3.2.2", r)

    def test_strict_gt_lt(self):
        r = ">1.0.0 <2.0.0"
        no("1.0.0", r)
        yes("1.0.1", r)
        no("2.0.0", r)

    def test_equality(self):
        yes("1.2.3", "1.2.3")
        yes("1.2.3", "=1.2.3")
        no("1.2.4", "1.2.3")

    def test_not_equal(self):
        yes("1.2.4", "!=1.2.3")
        no("1.2.3", "!=1.2.3")

    def test_separators_equivalent(self):
        for r in (">=1.0.0 <2.0.0", ">=1.0.0,<2.0.0", ">=1.0.0&&<2.0.0",
                  " >=1.0.0 , <2.0.0 "):
            yes("1.5.0", r)
            no("2.0.0", r)

    def test_empty_and_wildcard(self):
        yes("1.0.0", "*")
        yes("99.99.99", "*")
        yes("0.0.1", "*")


class TestCaret:
    @pytest.mark.parametrize("v", ["1.2.3", "1.9.9"])
    def test_caret_major(self, v):
        yes(v, "^1.2.3")

    def test_caret_major_bounds(self):
        r = "^1.2.3"
        no("1.2.2", r)
        yes("1.2.3", r)
        yes("1.9.0", r)
        no("2.0.0", r)

    def test_caret_zero_minor(self):
        r = "^0.2.3"
        yes("0.2.3", r)
        yes("0.2.9", r)
        no("0.2.2", r)
        no("0.3.0", r)
        no("0.2.3-0", r)  # 预发布低于下界

    def test_caret_zero_patch(self):
        r = "^0.0.3"
        yes("0.0.3", r)
        no("0.0.2", r)
        no("0.0.4", r)
        no("0.1.0", r)


class TestTilde:
    def test_tilde_bounds(self):
        r = "~3.1.0"
        no("3.0.9", r)
        yes("3.1.0", r)   # 下界含
        yes("3.1.9", r)
        no("3.2.0", r)    # 次版本变化即排除

    def test_tilde_with_prerelease_lower_bound(self):
        no("3.1.0-alpha", "~3.1.0")
        yes("3.1.0", "~3.1.0")


class TestPrereleasePolicy:
    def test_wildcard_never_matches_prerelease(self):
        no("4.0.0-beta.1", "*")

    def test_prerelease_window_same_core(self):
        r = ">=1.2.0-alpha <1.2.0"
        yes("1.2.0-alpha", r)
        yes("1.2.0-beta.5", r)
        yes("1.2.0-rc.99", r)
        no("1.1.9", r)
        no("1.2.0", r)           # 稳定版在上界外
        no("1.2.0-alpha", ">=1.2.0 <1.3.0")  # 比较器参照非预发布，门不开

    def test_prerelease_gate_requires_same_core(self):
        # 即便 alpha 数值上满足 <2.0.0，>=1.0.0 的参照核心是 1.0.0
        no("2.0.0-alpha", ">=1.0.0 <2.0.0")
        # 但下界显式带 alpha 时，同核心预发布可过
        yes("1.0.0-beta", ">=1.0.0-alpha <2.0.0")

    def test_exact_prerelease_match(self):
        r = "=2.0.0-rc.1"
        yes("2.0.0-rc.1", r)
        no("2.0.0-rc.2", r)
        no("2.0.0", r)

    def test_or_ranges_not_supported_inline(self):
        with pytest.raises(RangeParseError):
            parse_range("1.0.0 || 2.0.0")

    def test_stable_against_prerelease_comparator(self):
        # 稳定版不受预发布门影响，纯数值比较
        yes("2.0.0", ">1.5.0-beta.1")
        no("1.0.0", ">1.5.0-beta.1")

    def test_build_metadata_ignored(self):
        yes("1.5.0+build7", ">=1.0.0 <2.0.0")


class TestParseErrors:
    @pytest.mark.parametrize(
        "text",
        [
            "||1.0.0",
            "1.2.x",
            "1.0 - 2.0",
            "~1",
            "~1.2",
            ">= 1.0.0",       # 操作符与版本间不得有空格
            ">=v1.0.0",
            "**",
            "=*",
            "* || >=1.0.0",
        ],
    )
    def test_reject_unsupported_syntax(self, text):
        with pytest.raises(RangeParseError):
            parse_range(text)

    def test_wildcard_cannot_combine(self):
        with pytest.raises(RangeParseError):
            parse_range("* >=1.0.0")
