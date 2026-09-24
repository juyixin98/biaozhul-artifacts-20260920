"""端到端核对测试：合并、变体、证据路径、循环、未知、错误 purl。"""
from __future__ import annotations

import json

import pytest

from app.engine import evidence_paths
from app.service import run_check
from app.sbom import SbomError, parse_cyclonedx


def _run(doc, vulns, fixture_meta, conn):
    return run_check(doc, vulns, fixture_meta, conn,
                     json.dumps(doc).encode("utf-8"))


def _by_purl(result, disposition):
    return {item["component"]["purl"]: item
            for item in result["results"][disposition]}


def _all_component_purls(result):
    return [item["component"]["purl"]
            for bucket in result["results"].values()
            for item in bucket]


def test_end_to_end_full_fixture(full_sbom, vulns, fixture_meta, conn):
    result = _run(full_sbom, vulns, fixture_meta, conn)

    affected = _by_purl(result, "affected")
    unknown = _by_purl(result, "unknown")
    not_aff = _by_purl(result, "not_affected")

    # 受影响：npm 范围内、预发布、pypi rc/稳定、maven 区间内、gem rc、deb
    assert "pkg:npm/left-pad@6.10.3" in affected
    assert "pkg:npm/pre-release-lib@1.0.0-rc.2" in affected
    assert "pkg:pypi/requests@2.20" in affected
    assert "pkg:pypi/requests@2.20rc1" in affected
    assert "pkg:maven/com.example/http-core@3.1.0" in affected
    assert "pkg:gem/nokogiri@1.13.0.rc1" in affected
    assert "pkg:deb/debian/curl@2.50.3-1?arch=amd64" in affected

    # 端点修复版本 / 已修复版本不受影响
    assert "pkg:npm/pre-release-lib@1.0.0" in not_aff
    assert "pkg:maven/com.example/http-core@3.2.0" in not_aff
    # PEP 440：2.21rc1 < 2.21，不被 <2.21 命中（不是字符串比较）
    assert "pkg:pypi/requests@2.21rc1" in not_aff

    # 不支持生态 -> 未知（明确理由），绝不做字符串比较
    ghost = "pkg:cargo/ghost@0.1.0"
    assert ghost in unknown
    match = unknown[ghost]["matched"][0]
    assert match["status"] == "unknown"
    assert match["reason_code"] == "unsupported_ecosystem"


def test_duplicate_components_merge_but_variants_dont(full_sbom, vulns,
                                                      fixture_meta, conn):
    result = _run(full_sbom, vulns, fixture_meta, conn)
    affected = result["results"]["affected"]
    lp = [a for a in affected if a["component"]["name"] == "left-pad"
          and a["component"]["qualifiers"] == {}]
    assert len(lp) == 1, "重复 purl 必须合并成一个节点"
    assert lp[0]["component"]["merged_duplicate_refs"] == [
        "left-pad@6.10.3-dup"]
    # scope 合并：required 压过重复项声明的 optional
    assert lp[0]["component"]["scope"] == "required"

    all_purls = _all_component_purls(result)
    assert "pkg:npm/left-pad@6.10.3?os=linux" in all_purls
    assert "pkg:npm/left-pad@6.10.3?os=darwin" in all_purls
    assert all_purls.count("pkg:npm/left-pad@6.10.3") == 1


def test_variant_specific_vuln_does_not_mix_variants(full_sbom, vulns,
                                                     fixture_meta, conn):
    result = _run(full_sbom, vulns, fixture_meta, conn)
    affected = _by_purl(result, "affected")

    linux = affected["pkg:npm/left-pad@6.10.3?os=linux"]["matched"]
    linux_ids = {m["vulnerability_id"] for m in linux}
    # 通用条目 + 仅 linux 变体条目
    assert {"SYNTH-NPM-2026-0001", "SYNTH-NPM-2026-0007"} <= linux_ids

    # 无 qualifier 的基础变体不命中 linux 专属条目
    base = affected["pkg:npm/left-pad@6.10.3"]["matched"]
    assert {m["vulnerability_id"] for m in base} == {"SYNTH-NPM-2026-0001"}

    # darwin 变体同样不命中 linux 专属条目
    darwin = affected["pkg:npm/left-pad@6.10.3?os=darwin"]["matched"]
    assert {m["vulnerability_id"] for m in darwin} == {"SYNTH-NPM-2026-0001"}


def test_transitive_evidence_paths(full_sbom, vulns, fixture_meta, conn):
    result = _run(full_sbom, vulns, fixture_meta, conn)
    affected = _by_purl(result, "affected")

    # curl 是 requests 的传递依赖：root -> requests -> curl
    curl = affected["pkg:deb/debian/curl@2.50.3-1?arch=amd64"]
    assert curl["is_transitive"] is True
    assert curl["reachable_from_direct"] is True
    joined = [" -> ".join(p["purls"]) for p in curl["evidence_paths"]]
    assert joined, "传递依赖必须给出证据路径"
    assert any(
        "pkg:npm/demo-app@1.0.0" in s
        and "pkg:pypi/requests@2.20" in s
        and s.endswith("pkg:deb/debian/curl@2.50.3-1?arch=amd64")
        for s in joined), joined

    # 直接依赖 left-pad：距离根 1
    lp = affected["pkg:npm/left-pad@6.10.3"]
    assert lp["is_transitive"] is False
    assert any(p["length"] == 1 and
               p["purls"][-1] == "pkg:npm/left-pad@6.10.3"
               for p in lp["evidence_paths"])

    # requests@2.20rc1 挂在 left-pad 下：root -> left-pad -> rc
    rc = affected["pkg:pypi/requests@2.20rc1"]
    assert rc["is_transitive"] is True
    assert any("pkg:npm/left-pad@6.10.3" in " -> ".join(p["purls"])
               for p in rc["evidence_paths"])


def test_diamond_produces_multiple_shortest_paths(conn, vulns, fixture_meta):
    doc = {
        "bomFormat": "CycloneDX", "specVersion": "1.5", "version": 1,
        "metadata": {"component": {
            "bom-ref": "root@1", "type": "application", "name": "root",
            "version": "1", "purl": "pkg:npm/root@1"}},
        "components": [
            {"bom-ref": "m1@1", "type": "library", "name": "m1",
             "version": "1.0.0", "purl": "pkg:npm/m1@1.0.0"},
            {"bom-ref": "m2@1", "type": "library", "name": "m2",
             "version": "1.0.0", "purl": "pkg:npm/m2@1.0.0"},
            {"bom-ref": "left-pad@6.10.3", "type": "library",
             "name": "left-pad", "version": "6.10.3",
             "purl": "pkg:npm/left-pad@6.10.3"},
        ],
        "dependencies": [
            {"ref": "root@1", "dependsOn": ["m1@1", "m2@1"]},
            {"ref": "m1@1", "dependsOn": ["left-pad@6.10.3"]},
            {"ref": "m2@1", "dependsOn": ["left-pad@6.10.3"]},
        ],
    }
    result = _run(doc, vulns, fixture_meta, conn)
    lp = _by_purl(result, "affected")["pkg:npm/left-pad@6.10.3"]
    paths = lp["evidence_paths"]
    assert len(paths) >= 2, "菱形依赖应给出多条等长最短路径"
    assert all(p["length"] == 2 for p in paths)


def test_cycles_detected_but_do_not_hang(full_sbom):
    report = parse_cyclonedx(full_sbom)
    cycles = report.cycles
    assert len(cycles) == 1
    cyc_purls = sorted(report._by_key[k].purl.canonical() for k in cycles[0])
    assert cyc_purls == ["pkg:npm/cycle-a@1.0.0", "pkg:npm/cycle-b@2.0.0"]
    # 有环图上证据路径搜索必须正常终止
    target = next(k for k, c in report._by_key.items()
                  if c.purl.name == "cycle-b")
    paths = evidence_paths(report, target)
    assert paths and all(p[-1] == target for p in paths)


def test_self_loop_is_a_cycle():
    doc = {
        "bomFormat": "CycloneDX", "specVersion": "1.5", "version": 1,
        "metadata": {"component": {
            "bom-ref": "r@1", "type": "application", "name": "r",
            "version": "1", "purl": "pkg:npm/r@1"}},
        "components": [{"bom-ref": "self@1.0.0", "type": "library",
                        "name": "self-dep", "version": "1.0.0",
                        "purl": "pkg:npm/self-dep@1.0.0"}],
        "dependencies": [
            {"ref": "r@1", "dependsOn": ["self@1.0.0"]},
            {"ref": "self@1.0.0", "dependsOn": ["self@1.0.0"]}],
    }
    report = parse_cyclonedx(doc)
    assert len(report.cycles) == 1
    key = next(k for k, c in report._by_key.items()
               if c.purl.name == "self-dep")
    assert report.cycles[0] == [key]


def test_invalid_purls_are_reported_not_fatal(full_sbom, vulns,
                                              fixture_meta, conn):
    result = _run(full_sbom, vulns, fixture_meta, conn)
    ignored = {(i["bom_ref"], i["reason"])
               for i in result["ignored_components"]}
    assert ("bad-purl-1", "invalid_purl") in ignored
    assert ("bad-purl-2", "invalid_purl") in ignored
    assert ("no-version-lib", "missing_version") in ignored
    assert result["summary"]["total_components"] > 0


@pytest.mark.parametrize("doc", [
    {"bomFormat": "SPDX", "specVersion": "1.5"},
    {"bomFormat": "CycloneDX", "specVersion": "1.2"},
    {"bomFormat": "CycloneDX"},
    {"bomFormat": "CycloneDX", "specVersion": "1.5",
     "components": "not-a-list"},
])
def test_unsupported_document_rejected(doc):
    with pytest.raises(SbomError):
        parse_cyclonedx(doc)


def test_non_object_document_rejected():
    with pytest.raises(SbomError):
        parse_cyclonedx(["not", "an", "object"])  # type: ignore[arg-type]


def test_summary_counts_and_fixture_disclaimer(full_sbom, vulns,
                                               fixture_meta, conn):
    result = _run(full_sbom, vulns, fixture_meta, conn)
    s = result["summary"]
    assert s["cycles_detected"] == 1
    assert s["duplicates_merged"] >= 1
    assert len(result["results"]["affected"]) == s["affected_components"]
    assert result["data_source"]["not_real_advisory"] is True
    assert "不覆盖最新漏洞" in result["data_source"]["notice"]
