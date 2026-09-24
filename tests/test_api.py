"""FastAPI 层测试：健康检查、核对、持久化、错误状态码、SHA-256。"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def test_health_reports_fixture_limitations(client):
    r = client.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert body["data_source"]["type"] == "local_fixture"
    assert body["data_source"]["not_real_advisory"] is True
    assert set(body["supported"]["ecosystems"]) == {
        "npm", "maven", "pypi", "gem", "deb"}


def test_check_minimal_sbom(client, minimal_sbom):
    # 用完全相同的原始字节发送与计算哈希
    raw = json.dumps(minimal_sbom).encode()
    r = client.post("/api/v1/sbom/check", content=raw,
                    headers={"content-type": "application/json"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["summary"]["affected_components"] == 0
    purls = [i["component"]["purl"]
             for i in body["results"]["not_affected"]]
    assert "pkg:npm/left-pad@6.11.3" in purls
    assert body["run"]["source_sha256"] == hashlib.sha256(raw).hexdigest()


def test_check_full_sbom_and_persist(client, full_sbom):
    r = client.post("/api/v1/sbom/check", json=full_sbom)
    assert r.status_code == 200, r.text
    run_id = r.json()["run"]["id"]

    # 取回持久化结果
    got = client.get(f"/api/v1/runs/{run_id}")
    assert got.status_code == 200
    assert got.json()["run"]["id"] == run_id

    # 列表 / 按结论筛选
    runs = client.get("/api/v1/runs")
    assert runs.status_code == 200
    assert any(row["id"] == run_id for row in runs.json()["runs"])

    aff = client.get("/api/v1/runs", params={"disposition": "affected"})
    assert aff.status_code == 200
    assert any(row["id"] == run_id for row in aff.json()["runs"])

    bad = client.get("/api/v1/runs", params={"disposition": "bogus"})
    assert bad.status_code == 400

    missing = client.get("/api/v1/runs/does-not-exist")
    assert missing.status_code == 404


def test_check_rejects_bad_json(client):
    r = client.post("/api/v1/sbom/check",
                    content=b"{not json",
                    headers={"content-type": "application/json"})
    assert r.status_code == 400


def test_check_rejects_empty_body(client):
    r = client.post("/api/v1/sbom/check", content=b"")
    assert r.status_code == 400


def test_check_422_for_unsupported_bom(client):
    r = client.post("/api/v1/sbom/check",
                    json={"bomFormat": "SPDX", "specVersion": "2.3"})
    assert r.status_code == 422
    assert "bomFormat" in r.json()["detail"]


def test_check_full_finds_expected_affected(client, full_sbom):
    body = client.post("/api/v1/sbom/check", json=full_sbom).json()
    ids = {m["vulnerability_id"]
           for item in body["results"]["affected"]
           for m in item["matched"]}
    assert {
        "SYNTH-NPM-2026-0001", "SYNTH-NPM-2026-0002", "SYNTH-MAVEN-2026-0003",
        "SYNTH-PYPI-2026-0004", "SYNTH-GEM-2026-0005", "SYNTH-DEB-2026-0006",
    } <= ids


def test_vulnerabilities_listing_is_marked_synthetic(client):
    body = client.get("/vulnerabilities").json()
    assert body["count"] >= 6
    assert "虚构" in body["notice"]
