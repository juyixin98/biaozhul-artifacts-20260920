"""各生态版本解析与范围比较测试。

这些用例验证的是结构化版本算法（含预发布与端点语义），
而非字符串大小比较——后者会在 9 vs 10、rc 顺序、dpkg '~' 等用例上失败。
"""
from __future__ import annotations

import pytest

from app import versions as v
from app.versions import (
    VersionError, deb_satisfies, gem_satisfies, maven_compare, maven_satisfies,
    npm_satisfies, pypi_satisfies, semver_compare, SemVer, dpkg_compare,
)


# --- npm / SemVer ---------------------------------------------------------

@pytest.mark.parametrize("ver,spec,expected", [
    ("6.10.3", "^5.0.0 || >=6.0.0 <6.11.3", True),
    ("5.9.0", "^5.0.0", True),
    ("6.11.3", ">=6.0.0 <6.11.3", False),       # 上端点开区间
    ("6.0.0", ">=6.0.0 <6.11.3", True),         # 下端点闭区间
    ("1.2.3", "1.2.3 - 2.3.4", True),
    ("2.3.4", "1.2.3 - 2.3.4", True),           # 连字符右端含
    ("2.3.5", "1.2.3 - 2.3.4", False),
    ("1.2.x", None, None),  # 占位，下面单独测
    ("10.2.3", "1.2.x", False),
    ("1.2.9", "1.2.x", True),
    ("1.3.0", "1.2.x", False),
    ("0.15.2", "^0.15.0", True),
    ("0.16.0", "^0.15.0", False),
    ("0.0.3", "^0.0.3", True),
    ("0.0.4", "^0.0.3", False),
    ("1.5.3", "~1.5.0", True),
    ("1.6.0", "~1.5.0", False),
])
def test_npm_ranges(ver, spec, expected):
    if spec is None:
        return
    assert npm_satisfies(ver, spec) is expected


def test_semver_prerelease_ordering():
    rc1 = SemVer.parse("1.0.0-rc.1")
    rc2 = SemVer.parse("1.0.0-rc.2")
    rel = SemVer.parse("1.0.0")
    assert semver_compare(rc1, rc2) < 0
    assert semver_compare(rc2, rel) < 0
    assert semver_compare(rel, rc1) > 0
    # 数字标识低于非数字标识：1-rc < 1-rc.1 之外，alpha < beta
    assert semver_compare(SemVer.parse("1.0.0-alpha"),
                          SemVer.parse("1.0.0-alpha.1")) < 0
    assert semver_compare(SemVer.parse("1.0.0-alpha.1"),
                          SemVer.parse("1.0.0-alpha.beta")) < 0
    assert semver_compare(SemVer.parse("1.0.0-alpha.beta"),
                          SemVer.parse("1.0.0-beta")) < 0


def test_npm_prerelease_gate():
    # 范围未出现任何 1.0.0 预发布比较器 -> 预发布版本不命中
    assert npm_satisfies("1.0.0-rc.2", "^1.0.0") is False
    assert npm_satisfies("1.0.0-rc.2", ">=1.0.0-rc.1 <2.0.0") is True
    # 1.5.0-rc.1 不属于 <1.5.0，且范围没有 1.5.0 预发布标识
    assert npm_satisfies("1.5.0-rc.1", ">=1.0.0 <1.5.0") is False
    # 不同 core 的预发布也不被裸范围接受
    assert npm_satisfies("2.0.0-alpha", ">=1.0.0 <3.0.0") is False


def test_numeric_semver_vs_string_order_is_structural():
    # 字符串比较会把 "10" < "9"；结构化比较必须正确
    assert semver_compare(SemVer.parse("10.0.0"),
                          SemVer.parse("9.9.9")) > 0
    assert npm_satisfies("10.0.0", ">=9.0.0") is True


def test_invalid_semver_rejected():
    with pytest.raises(VersionError):
        SemVer.parse("01.2.3")
    with pytest.raises(VersionError):
        npm_satisfies("not-a-version", "^1.0.0")


# --- Maven ----------------------------------------------------------------

@pytest.mark.parametrize("ver,spec,expected", [
    ("3.1.0", "[3.0.0,3.2.0)", True),
    ("3.0.0", "[3.0.0,3.2.0)", True),
    ("3.2.0", "[3.0.0,3.2.0)", False),
    ("3.2.0", "[3.0.0,3.2.0]", True),
    ("3.1.0-rc1", "[3.0.0,3.2.0)", True),
    ("3.0.0-alpha-1", "[3.0.0,)", False),
    ("3.0.0", "[3.0.0,)", True),
    ("4.0.0", "(,3.2.0]", False),
    ("3.2.0-sp1", "[3.0.0,3.2.0]", False),
    ("3.2.0", "3.2.0", True),      # 普通版本 = 精确
])
def test_maven_ranges(ver, spec, expected):
    assert maven_satisfies(ver, spec) is expected


def test_maven_qualifier_ordering():
    order = ["3.0.0-alpha-1", "3.0.0-alpha-2", "3.0.0-beta-1",
             "3.0.0-milestone-1", "3.0.0-rc-1", "3.0.0-SNAPSHOT",
             "3.0.0", "3.0.0-sp-1"]
    for a, b in zip(order, order[1:]):
        assert maven_compare(a, b) < 0, f"{a} should precede {b}"
        assert maven_compare(b, a) > 0
    assert maven_compare("3.0", "3.0.0") == 0


def test_maven_numeric_structural_order():
    assert maven_compare("3.10.0", "3.9.0") > 0


# --- PyPI / PEP 440 -------------------------------------------------------

@pytest.mark.parametrize("ver,spec,expected", [
    ("2.20", ">=1.0,<2.21", True),
    ("2.21", ">=1.0,<2.21", False),
    ("2.21rc1", ">=1.0,<2.21", False),
    ("2.21rc1", ">=1.0,<2.22", True),
    ("2.20.1", "~=2.20", True),
    ("2.21", "~=2.20", True),   # ~=2.20 等价于 >=2.20,==2.*
    ("3.0.0", "~=2.20", False),
    ("2.20", "==2.20", True),
    ("2.21", "!=2.21", False),
    ("10.0", ">=9.0", True),   # 非字符串比较
])
def test_pypi_ranges(ver, spec, expected):
    assert pypi_satisfies(ver, spec) is expected


def test_pypi_invalid_version():
    with pytest.raises(VersionError):
        pypi_satisfies("not a version", ">=1.0")


# --- RubyGems -------------------------------------------------------------

@pytest.mark.parametrize("ver,spec,expected", [
    ("1.12.4", ">=1.0,<1.13.0", True),
    ("1.13.0", ">=1.0,<1.13.0", False),
    ("1.13.0.rc1", ">=1.0,<1.13.0", True),
    ("1.2.5", "~>1.2.0", True),
    ("1.2.0", "~>1.2.0", True),
    ("1.3.0", "~>1.2.0", False),
    ("1.9.9", "~>1.2", True),
    ("2.0.0", "~>1.2", False),
])
def test_gem_ranges(ver, spec, expected):
    assert gem_satisfies(ver, spec) is expected


def test_gem_prerelease_structural_order():
    # 1.13.0.rc1 < 1.13.0；字符串比较无法表达这一点
    assert gem_satisfies("1.13.0.rc1", "<1.13.0") is True
    with pytest.raises(VersionError):
        gem_satisfies("garbage??", ">=1.0")


# --- dpkg / Debian --------------------------------------------------------

@pytest.mark.parametrize("ver,spec,expected", [
    ("2.50.3-1", ">=2.30,<2.50.4-1", True),
    ("2.50.4-1", ">=2.30,<2.50.4-1", False),   # 上端点开
    ("2.50.4~rc1-1", ">=2.30,<2.50.4-1", True),
    ("1:1.0", ">=9.0", True),                  # epoch 主导
    ("2.0-pre1", ">=2.0", True),               # '.'/'-' 非字母后缀高于串尾
    ("1.0~rc1", ">=1.0", False),               # '~' 低于一切
])
def test_deb_ranges(ver, spec, expected):
    assert deb_satisfies(ver, spec) is expected


def test_dpkg_reference_ordering():
    # 与系统 dpkg(1) --compare-versions 一致的关键序关系
    assert dpkg_compare("7.0", "7.0.0") < 0
    assert dpkg_compare("1.0a", "1.0-1") > 0
    assert dpkg_compare("1.0-0", "1.0") == 0
    assert dpkg_compare("0~20160101", "0") < 0


def test_deb_invalid_version():
    with pytest.raises(VersionError):
        dpkg_compare("-bad", "1.0")


# --- 统一入口：未知语义 ----------------------------------------------------

def test_evaluate_unknowns():
    assert v.evaluate("npm", None, "^1.0.0", "range") == (False, "missing_version")
    assert v.evaluate("npm", "", "^1.0.0", "range") == (False, "missing_version")
    assert v.evaluate("cargo", "1.0", "^1", "range") == (False, "unsupported_ecosystem")
    assert v.evaluate("npm", "nonsense", "^1.0.0", "range") == (False, "invalid_version")


def test_evaluate_normal():
    affected, reason = v.evaluate("npm", "1.2.0", "^1.0.0", "range")
    assert affected is True and reason is None
    affected, reason = v.evaluate("npm", "2.0.0", "^1.0.0", "range")
    assert affected is False and reason is None
