"""SemVer 2.0.0 解析与优先级测试。"""
from __future__ import annotations

import pytest

from sbom_risk.semver import VersionParseError, parse_version


@pytest.mark.parametrize(
    "text",
    [
        "0.0.0",
        "1.2.3",
        "10.20.30",
        "1.0.0-alpha",
        "1.0.0-alpha.1",
        "1.0.0-0.3.7",
        "1.0.0-x.7.z.92",
        "1.0.0-alpha+001",
        "1.0.0+20130313144700",
        "1.0.0-beta+exp.sha.5114f85",
        "1.0.0+21AF26D3----117B344092BD",
        "1.0.0-x-y-z.-",
    ],
)
def test_valid_versions_parse(text):
    v = parse_version(text)
    # 往返：构建元数据保留，核心+预发布往返一致
    assert str(v).split("+")[0] == text.split("+")[0]


@pytest.mark.parametrize(
    "text",
    [
        "1",
        "1.2",
        "1.2.3.4",
        "01.2.3",
        "1.02.3",
        "1.2.03",
        "v1.2.3",
        "1.2.3-",
        "1.2.3-alpha..1",
        "1.2.3-",
        "",
        "latest",
        "1.2.3-01",  # 数字预发布标识禁止前导零
        "1.2.3+",
        "=1.2.3",
        "1.2.3_beta",
    ],
)
def test_invalid_versions_raise(text):
    with pytest.raises(VersionParseError):
        parse_version(text)


def test_prerelease_identifier_ordering():
    # 官方规范 11 条优先级示例
    ordered = [
        "1.0.0-alpha",
        "1.0.0-alpha.1",
        "1.0.0-alpha.beta",
        "1.0.0-beta",
        "1.0.0-beta.2",
        "1.0.0-beta.11",
        "1.0.0-rc.1",
        "1.0.0",
    ]
    versions = [parse_version(s) for s in ordered]
    for a, b in zip(versions, versions[1:]):
        assert a < b, f"{a} 应小于 {b}"


def test_prerelease_lower_than_stable():
    assert parse_version("1.0.0-alpha") < parse_version("1.0.0")
    assert parse_version("1.0.0-1") < parse_version("1.0.0")


def test_build_metadata_ignored_in_comparison():
    assert parse_version("1.0.0+build1") == parse_version("1.0.0+build2")
    assert parse_version("1.0.0-alpha+exp.1") == parse_version("1.0.0-alpha")
    assert not (parse_version("1.0.0+a") < parse_version("1.0.0+b"))


def test_numeric_prerelease_ids_compared_as_integers():
    # beta.11 > beta.2（数值比较而非字典序）
    assert parse_version("1.0.0-beta.11") > parse_version("1.0.0-beta.2")


def test_numeric_prerelease_id_less_than_alphanumeric():
    assert parse_version("1.0.0-999") < parse_version("1.0.0-aaa")
    assert parse_version("1.0.0-alpha.1") < parse_version("1.0.0-alpha.beta")


def test_patch_zero_increment():
    assert parse_version("0.0.3") < parse_version("0.0.4")
