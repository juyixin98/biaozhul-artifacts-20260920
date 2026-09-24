"""端到端验收：HTTP 接口 + 夹具馈送 + 验收 SBOM。

覆盖验收要求：
- 传递依赖环（2 节点环 + 自环）；
- 同名包多个版本并存，且同名不同生态不混淆；
- 区间边界（含闭区间端点、脱字符、波浪号、预发布窗口、精确预发布、通配）；
- 受影响路径枚举（left-pad@1.3.0 有 2 条根路径）；
- 未知版本状态（非 SemVer 版本、情报缺区间）。
"""
from __future__ import annotations

import json

import pytest


def _index(findings):
    return {(f["package"]["id"], f["vulnerability"]["id"]): f for f in findings}


@pytest.fixture(scope="class")
def report(client, repo_root):
    sbom = json.loads((repo_root / "fixtures" / "sbom.acceptance.json").read_text())
    resp = client.post("/api/v1/match", json=sbom)
    assert resp.status_code == 200, resp.text
    return resp.json()


class TestAcceptance:
    def test_summary_counts(self, report):
        s = report["summary"]
        assert s["packages_total"] == 33
        assert s["affected_packages"] == 13
        assert s["affected_findings"] == 13
        assert s["unknown_findings"] == 3
        assert s["not_affected_packages"] == 11
        assert s["cycles_count"] == 2

    def test_feed_meta_verified(self, report):
        assert report["feed"]["verified"] is True
        assert report["feed"]["vulnerability_count"] == 8

    def test_transitive_dependency_cycles(self, report):
        cycles = [set(c["nodes"]) for c in report["cycles"]]
        assert {"npm:cycle-a@1.5.0", "npm:cycle-b@1.9.9"} in cycles
        assert {"npm:selfish@1.0.0"} in cycles  # 自环

    def test_affected_paths_through_cycle(self, report):
        idx = _index(report["findings"])
        f = idx[("npm:cycle-b@1.9.9", "VULN-CYCLE-B-9")]
        assert f["status"] == "affected"
        assert f["paths"] == [
            ["npm:app-root@1.0.0", "npm:cycle-a@1.5.0", "npm:cycle-b@1.9.9"]
        ]

    def test_multiple_paths_for_shared_transitive(self, report):
        idx = _index(report["findings"])
        f = idx[("npm:left-pad@1.3.0", "VULN-LEFTPAD-001")]
        assert f["status"] == "affected"
        assert sorted(f["paths"]) == sorted([
            ["npm:app-root@1.0.0", "npm:left-pad@1.3.0"],
            ["npm:app-root@1.0.0", "npm:mid-helper@2.0.0", "npm:left-pad@1.3.0"],
        ])
        assert f["matched_ranges"] == [">=1.0.0 <2.0.0"]

    @pytest.mark.parametrize("version,expected", [
        ("1.3.0", "affected"),      # 区间内
        ("0.9.0", None),            # 低于下界，not_affected 不出现在 findings
        ("2.0.0", None),            # 上界不含
        ("2.0.0-alpha", None),      # 预发布门：>=1.0.0 参照核心不匹配
        ("1.0", "unknown"),         # 非 SemVer
    ])
    def test_left_pad_versions(self, report, version, expected):
        idx = _index(report["findings"])
        key = (f"npm:left-pad@{version}", "VULN-LEFTPAD-001")
        if expected is None:
            assert key not in idx
        else:
            assert idx[key]["status"] == expected
            if expected == "unknown":
                assert idx[key]["reason"] == "invalid_semver"

    @pytest.mark.parametrize("version,expected", [
        ("3.0.9", None),
        ("3.1.0", "affected"),      # ~3.1.0 下界含
        ("3.1.9", "affected"),
        ("3.2.0", "affected"),      # 第二个区间 >=3.2.0 <=3.2.1
        ("3.2.1", "affected"),      # 闭区间上界含
        ("3.2.2", None),            # 闭区间上界外
    ])
    def test_closed_interval_boundaries_multi_lib(self, report, version, expected):
        idx = _index(report["findings"])
        key = (f"npm:multi-lib@{version}", "VULN-MULTI-7")
        if expected is None:
            assert key not in idx
        else:
            assert idx[key]["status"] == expected

    @pytest.mark.parametrize("version,expected", [
        ("1.2.0-alpha", "affected"),
        ("1.2.0-beta.5", "affected"),
        ("1.1.9", None),
        ("1.2.0", None),            # <1.2.0 排除稳定版
        ("2.0.0-rc.1", "affected"), # 精确预发布区间
        ("2.0.0-rc.2", None),
    ])
    def test_prerelease_window(self, report, version, expected):
        idx = _index(report["findings"])
        key = (f"npm:pre-lib@{version}", "VULN-PRE-42")
        if expected is None:
            assert key not in idx
        else:
            assert idx[key]["status"] == expected

    def test_prerelease_range_boundary_cycle_a(self, report):
        idx = _index(report["findings"])
        assert idx[("npm:cycle-a@1.0.0-beta.2", "VULN-CYCLE-A-1")]["status"] == "affected"
        assert ("npm:cycle-a@2.0.0", "VULN-CYCLE-A-1") not in idx  # 上界不含

    def test_wildcard_stable_but_not_prerelease_or_garbage(self, report):
        idx = _index(report["findings"])
        assert idx[("npm:weird-x@3.0.0", "VULN-WILDCARD-3")]["status"] == "affected"
        # 预发布与非法版本 * 不覆盖
        assert ("npm:weird-x@4.0.0-beta.1", "VULN-WILDCARD-3") not in idx
        f = idx[("npm:weird-x@01.2.3", "VULN-WILDCARD-3")]
        assert f["status"] == "unknown"
        assert f["reason"] == "invalid_semver"

    def test_vulnerability_without_range_is_unknown(self, report):
        idx = _index(report["findings"])
        f = idx[("npm:no-range-lib@5.5.5", "VULN-NORANGE-5")]
        assert f["status"] == "unknown"
        assert f["reason"] == "vulnerability_without_range"

    def test_same_name_different_ecosystems_not_mixed(self, report):
        idx = _index(report["findings"])
        # pypi:json 命中 pypi 情报
        assert idx[("pypi:json@1.5.0", "VULN-JSON-PYPI-2")]["status"] == "affected"
        # npm:json 对该情报完全不评估（不出现在 findings）
        assert ("npm:json@1.5.0", "VULN-JSON-PYPI-2") not in idx

    def test_unreachable_package_not_flagged_reachable(self, report, client, repo_root):
        # 孤立包（无任何根可达）：受影响但 reachable=False
        sbom = json.loads((repo_root / "fixtures" / "sbom.acceptance.json").read_text())
        sbom["packages"].append({
            "ecosystem": "npm", "name": "left-pad", "version": "1.5.0", "dependencies": []
        })
        resp = client.post("/api/v1/match", json=sbom)
        assert resp.status_code == 200
        idx = _index(resp.json()["findings"])
        f = idx[("npm:left-pad@1.5.0", "VULN-LEFTPAD-001")]
        assert f["status"] == "affected"
        assert f["reachable_from_roots"] is False
        assert f["paths"] == []


class TestHTTPErrors:
    def test_healthz(self, client):
        resp = client.get("/healthz")
        assert resp.status_code == 200
        assert resp.json() == {"status": "ok", "service_version": "1.0.0", "feed_verified": True}

    def test_feed_info(self, client):
        resp = client.get("/api/v1/feed/info")
        assert resp.status_code == 200
        data = resp.json()
        assert data["verified"] is True
        assert data["vulnerability_count"] == 8
        assert any(v["id"] == "VULN-LEFTPAD-001" for v in data["vulnerabilities"])

    def test_minimal_example(self, client, repo_root):
        sbom = json.loads((repo_root / "examples" / "sbom.minimal.json").read_text())
        resp = client.post("/api/v1/match", json=sbom)
        assert resp.status_code == 200
        data = resp.json()
        idx = _index(data["findings"])
        f = idx[("npm:left-pad@1.3.0", "VULN-LEFTPAD-001")]
        assert f["status"] == "affected"
        assert f["paths"] == [["npm:demo-app@0.1.0", "npm:left-pad@1.3.0"]]

    def test_dangling_dependency_returns_422(self, client):
        resp = client.post("/api/v1/match", json={
            "packages": [
                {"ecosystem": "npm", "name": "a", "version": "1.0.0",
                 "dependencies": ["npm:ghost@1.0.0"]}
            ]
        })
        assert resp.status_code == 422
        assert resp.json()["error"] == "invalid_sbom_graph"

    def test_bad_sbom_schema_returns_422(self, client):
        resp = client.post("/api/v1/match", json={"packages": [
            {"ecosystem": "", "name": "x", "version": "1.0.0"}
        ]})
        assert resp.status_code == 422

    def test_empty_sbom_ok(self, client):
        resp = client.post("/api/v1/match", json={"packages": []})
        assert resp.status_code == 200
        data = resp.json()
        assert data["findings"] == []
        assert data["summary"]["packages_total"] == 0
