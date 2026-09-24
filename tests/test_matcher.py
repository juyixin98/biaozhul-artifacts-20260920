"""匹配核心测试：依赖环、多版本、边界、生态隔离、未知版本。"""
import json
from pathlib import Path

import pytest

from app.main import load_fixture_vulnerabilities
from app.matcher import build_graph, find_paths, match_sbom
from app.models import SBOM

EXAMPLES = Path(__file__).resolve().parent.parent / "examples"


def load_example(name: str) -> SBOM:
    return SBOM.model_validate(json.loads((EXAMPLES / name).read_text(encoding="utf-8")))


@pytest.fixture(scope="module")
def vulns():
    return load_fixture_vulnerabilities()


def matches_for(report, component_id):
    return [m for m in report["matches"] if m["component_id"] == component_id]


class TestCycleGraph:
    def test_cycle_detected(self, vulns):
        sbom = load_example("sbom_cycle.json")
        report = match_sbom(sbom, vulns)
        assert report["summary"]["cycles_detected"] == 1
        cycle = report["cycles"][0]
        assert set(cycle) == {"a", "b", "c"}

    def test_vulnerable_component_behind_cycle_has_path(self, vulns):
        sbom = load_example("sbom_cycle.json")
        report = match_sbom(sbom, vulns)
        hits = matches_for(report, "dv")
        assert len(hits) == 1
        assert hits[0]["vulnerability_id"] == "VULN-2026-0005"
        paths = hits[0]["dependency_paths"]
        # 从 root 出发的简单路径：root→a→b→c→dv，环 a→b→c→a 不会导致死循环
        assert ["root", "a", "b", "c", "dv"] in paths
        for path in paths:
            assert len(path) == len(set(path)), "路径不应重复经过节点"

    def test_pure_cycle_graph_still_reports_paths(self, vulns):
        sbom = SBOM.model_validate(
            {
                "format": "sbom-matcher/1",
                "components": [
                    {"id": "x", "name": "pkg-x", "ecosystem": "npm", "version": "1.0.0",
                     "dependencies": ["y"]},
                    {"id": "y", "name": "deep-vuln", "ecosystem": "npm", "version": "2.0.0",
                     "dependencies": ["x"]},
                ],
            }
        )
        report = match_sbom(sbom, vulns)
        hits = matches_for(report, "y")
        assert len(hits) == 1
        assert hits[0]["dependency_paths"], "纯环图也应给出路径"
        assert any("没有入度为 0" in w for w in report["warnings"])


class TestBoundariesAndMultipleVersions:
    def test_same_package_multiple_versions(self, vulns):
        sbom = load_example("sbom_boundaries.json")
        report = match_sbom(sbom, vulns)
        # 同名同生态、不同版本：1.4.1 受影响，1.4.2（修复版）不受影响
        assert [m["vulnerability_id"] for m in matches_for(report, "net-npm-old")] == [
            "VULN-2026-0001"
        ]
        assert matches_for(report, "net-npm-fixed") == []

    def test_range_boundaries(self, vulns):
        sbom = load_example("sbom_boundaries.json")
        report = match_sbom(sbom, vulns)
        # 下界 1.0.0 包含，上界 2.0.0 排除
        assert len(matches_for(report, "bl-lower")) == 1
        assert matches_for(report, "bl-upper") == []

    def test_prerelease_range(self, vulns):
        sbom = load_example("sbom_boundaries.json")
        report = match_sbom(sbom, vulns)
        # 2.0.0-rc.1 命中 ">=2.0.0-alpha <2.0.0"；正式版 2.0.0 不命中
        assert [m["vulnerability_id"] for m in matches_for(report, "libtrans-pre")] == [
            "VULN-2026-0003"
        ]
        assert matches_for(report, "libtrans-release") == []


class TestEcosystemIsolation:
    def test_same_name_different_ecosystem_not_merged(self, vulns):
        sbom = load_example("sbom_boundaries.json")
        report = match_sbom(sbom, vulns)
        pypi_hits = matches_for(report, "net-pypi")
        # pypi 的 acme-net 只命中 pypi 漏洞，绝不命中 npm 同名漏洞
        assert [m["vulnerability_id"] for m in pypi_hits] == ["VULN-2026-0002"]
        npm_hits = matches_for(report, "net-npm-old")
        assert all(m["vulnerability_id"] != "VULN-2026-0002" for m in npm_hits)


class TestUnknownVersion:
    def test_missing_version_is_unknown(self, vulns):
        sbom = load_example("sbom_boundaries.json")
        report = match_sbom(sbom, vulns)
        entry = next(u for u in report["unknown"] if u["component_id"] == "mystery")
        assert entry["reason"] == "missing_version"
        assert entry["status"] == "unknown"
        # 未知版本不判受影响，但列出可能相关的漏洞
        assert entry["possible_vulnerabilities"] == ["VULN-2026-0001"]
        assert matches_for(report, "mystery") == []

    def test_unparseable_version_is_unknown(self, vulns):
        sbom = load_example("sbom_boundaries.json")
        report = match_sbom(sbom, vulns)
        entry = next(u for u in report["unknown"] if u["component_id"] == "weird")
        assert entry["reason"] == "invalid_semver"
        assert entry["possible_vulnerabilities"] == ["VULN-2026-0003"]


class TestGraphUtilities:
    def test_dangling_dependency_warns(self):
        sbom = SBOM.model_validate(
            {
                "format": "sbom-matcher/1",
                "components": [
                    {"id": "r", "name": "app", "ecosystem": "npm", "version": "1.0.0",
                     "dependencies": ["ghost"]}
                ],
            }
        )
        graph = build_graph(sbom)
        assert any("ghost" in w for w in graph.warnings)

    def test_find_paths_cap(self):
        # 构造菱形依赖大量分叉，验证路径数量被封顶
        components = [
            {"id": "root", "name": "app", "ecosystem": "npm", "version": "1.0.0",
             "dependencies": [f"m{i}" for i in range(10)]},
        ]
        for i in range(10):
            components.append(
                {"id": f"m{i}", "name": f"pkg-{i}", "ecosystem": "npm", "version": "1.0.0",
                 "dependencies": ["target"]}
            )
        components.append(
            {"id": "target", "name": "deep-vuln", "ecosystem": "npm", "version": "1.0.0"}
        )
        sbom = SBOM.model_validate({"format": "sbom-matcher/1", "components": components})
        graph = build_graph(sbom)
        paths = find_paths(graph, "target", cap=5)
        assert len(paths) == 5
