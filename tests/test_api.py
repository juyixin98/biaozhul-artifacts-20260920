"""End-to-end HTTP tests using FastAPI's TestClient."""

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.signing import verify_report

client = TestClient(app)

DANGEROUS = """
func load() { return input(); }
func main() {
    var x = load();
    sink(x);
}
"""

SAFE = """
func load() { return escape(input()); }
func main() {
    sink(load());
}
"""


def test_health():
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_signing_key_is_ed25519_pem():
    r = client.get("/signing-key")
    assert r.status_code == 200
    body = r.json()
    assert body["algorithm"] == "Ed25519"
    assert "BEGIN PUBLIC KEY" in body["public_key_pem"]


def test_analyze_dangerous_signed_report():
    r = client.post("/analyze", json={"code": DANGEROUS})
    assert r.status_code == 200
    report = r.json()
    assert report["verdict"] == "vulnerable"
    assert report["finding_count"] == 1
    f = report["findings"][0]
    assert f["sink"] == "sink"
    assert f["evidence_paths"][0]["source"] == "input"
    # signature is present and verifies
    assert report["signature"]["algorithm"] == "Ed25519"
    assert verify_report(report) is True


def test_analyze_safe():
    r = client.post("/analyze", json={"code": SAFE})
    report = r.json()
    assert report["verdict"] == "safe"
    assert verify_report(report) is True


def test_tampered_report_fails_verification():
    r = client.post("/analyze", json={"code": DANGEROUS})
    report = r.json()
    report["verdict"] = "safe"  # attacker downgrades the verdict
    assert verify_report(report) is False


def test_verify_endpoint():
    r = client.post("/analyze", json={"code": DANGEROUS})
    report = r.json()
    ok = client.post("/verify", json={"report": report})
    assert ok.json() == {"valid_signature": True, "verdict": "vulnerable"}


def test_parse_error_returns_400():
    r = client.post("/analyze", json={"code": "func main( { }"})
    assert r.status_code == 400
    assert r.json()["verdict"] == "parse_error"


def test_empty_code_rejected():
    r = client.post("/analyze", json={"code": "   "})
    assert r.status_code == 422


def test_custom_policy_extension():
    code = """
    func main() {
        var x = fetch();
        sink(cleanup(x));
    }
    """
    r = client.post("/analyze", json={
        "code": code,
        "sources": ["fetch"],
        "sanitizers": ["cleanup"],
    })
    assert r.json()["verdict"] == "safe"


def test_openapi_available():
    r = client.get("/openapi.json")
    assert r.status_code == 200
    assert "/analyze" in r.json()["paths"]
