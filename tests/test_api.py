"""HTTP 接口端到端测试（fastapi TestClient）。"""
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.main import app

EXAMPLES = Path(__file__).resolve().parent.parent / "examples"
client = TestClient(app)


def load_example(name: str) -> dict:
    return json.loads((EXAMPLES / name).read_text(encoding="utf-8"))


def test_health():
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


def test_list_fixture_vulnerabilities():
    resp = client.get("/v1/vulnerabilities")
    assert resp.status_code == 200
    body = resp.json()
    assert body["count"] == 5
    assert body["digest"].startswith("sha256:")
    ecosystems = {(v["package"]["ecosystem"], v["package"]["name"]) for v in body["vulnerabilities"]}
    assert ("npm", "acme-net") in ecosystems
    assert ("pypi", "acme-net") in ecosystems


def test_match_cycle_example_with_default_fixtures():
    resp = client.post("/v1/match", json={"sbom": load_example("sbom_cycle.json")})
    assert resp.status_code == 200
    body = resp.json()
    assert body["summary"]["affected"] == 1
    assert body["summary"]["cycles_detected"] == 1
    hit = body["matches"][0]
    assert hit["vulnerability_id"] == "VULN-2026-0005"
    assert ["root", "a", "b", "c", "dv"] in hit["dependency_paths"]
    assert body["sbom_digest"].startswith("sha256:")
    assert body["signature"].startswith("hmac-sha256:")


def test_match_boundaries_example():
    resp = client.post("/v1/match", json={"sbom": load_example("sbom_boundaries.json")})
    assert resp.status_code == 200
    body = resp.json()
    by_component = {}
    for m in body["matches"]:
        by_component.setdefault(m["component_id"], []).append(m["vulnerability_id"])
    assert by_component == {
        "net-npm-old": ["VULN-2026-0001"],
        "net-pypi": ["VULN-2026-0002"],
        "libtrans-pre": ["VULN-2026-0003"],
        "bl-lower": ["VULN-2026-0004"],
    }
    unknown_ids = {u["component_id"] for u in body["unknown"]}
    assert unknown_ids == {"mystery", "weird"}
    assert body["summary"]["affected"] == 4
    assert body["summary"]["unknown"] == 2


def test_match_with_inline_vulnerabilities_override():
    sbom = load_example("sbom_boundaries.json")
    payload = {
        "sbom": sbom,
        "vulnerabilities": [
            {
                "id": "CUSTOM-1",
                "package": {"ecosystem": "npm", "name": "acme-net"},
                "ranges": ["<1.0.0"],
                "fixed_versions": [],
            }
        ],
    }
    resp = client.post("/v1/match", json=payload)
    assert resp.status_code == 200
    body = resp.json()
    # 自定义漏洞只覆盖 <1.0.0，所有 acme-net 组件均不命中
    assert body["matches"] == []
    assert body["summary"]["vulnerabilities_checked"] == 1


def test_invalid_range_in_vulnerability_warns_not_crashes():
    payload = {
        "sbom": load_example("sbom_cycle.json"),
        "vulnerabilities": [
            {
                "id": "BAD-1",
                "package": {"ecosystem": "npm", "name": "deep-vuln"},
                "ranges": ["1.2.3 - 2.0.0"],
            }
        ],
    }
    resp = client.post("/v1/match", json=payload)
    assert resp.status_code == 200
    body = resp.json()
    assert body["matches"] == []
    assert any("BAD-1" in w for w in body["warnings"])


def test_invalid_sbom_rejected_with_422():
    resp = client.post("/v1/match", json={"sbom": {"components": []}})
    assert resp.status_code == 422


def test_signature_is_deterministic():
    payload = {"sbom": load_example("sbom_cycle.json")}
    a = client.post("/v1/match", json=payload).json()
    b = client.post("/v1/match", json=payload).json()
    assert a["signature"] == b["signature"]
    assert a["sbom_digest"] == b["sbom_digest"]
