"""End-to-end API tests against the FastAPI app with a real SQLite DB."""
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SAMPLE = ROOT / "examples" / "sample-app.bom.json"
MINIMAL = ROOT / "examples" / "minimal.bom.json"


def _post(client, path: Path):
    return client.post("/api/v1/sboms/analyze",
                       content=path.read_bytes(),
                       headers={"content-type": "application/json"})


def test_healthz_and_fixture_disclaimer(client):
    r = client.get("/healthz")
    assert r.status_code == 200
    assert r.json()["fixture_advisories"] >= 7
    fx = client.get("/api/v1/fixture").json()
    assert fx["live_feed"] is False
    assert "not a live feed" in fx["warning"]


def test_minimal_fixed_version_is_not_affected(client):
    r = _post(client, MINIMAL)
    assert r.status_code == 201, r.text
    body = r.json()
    statuses = {(f["component"], f["version"], f["status"]) for f in body["findings"]}
    assert ("left-pad-lite", "1.4.2", "not_affected") in statuses
    assert body["summary"]["affected_findings"] == 0


def test_sample_scan_full(client):
    r = _post(client, SAMPLE)
    assert r.status_code == 201, r.text
    body = r.json()
    findings = body["findings"]

    def status_of(ecosystem, name, version):
        return {(f["status"], f.get("vulnerability_id"))
                for f in findings
                if f["ecosystem"] == ecosystem and f["component"] == name
                and f["version"] == version}

    # ---- direct, affected, inclusive endpoints
    assert ("affected", "NPM-EXAMPLE-1001") in status_of("npm", "left-pad-lite", "1.4.0")
    # fixed version present as a separate component -> not affected
    assert ("not_affected", "NPM-EXAMPLE-1001") in status_of("npm", "left-pad-lite", "1.5.0")

    # ---- prerelease boundary
    assert ("affected", "NPM-EXAMPLE-1002") in status_of("npm", "range-toy", "2.0.0-rc.1")

    # ---- caret 0.x
    assert ("affected", "NPM-EXAMPLE-1003") in status_of("npm", "caret-lib", "0.2.5")

    # ---- pypi prerelease + final boundary
    assert ("affected", "PYSEC-EXAMPLE-2002") in status_of("pypi", "wheelmaker", "2.0b1")
    assert ("not_affected", "PYSEC-EXAMPLE-2002") in status_of("pypi", "wheelmaker", "2.0")

    # ---- maven exclusive upper endpoint
    assert ("affected", "GHSA-EXAMPLE-3001") in status_of("maven", "common-core", "1.4.0")

    # ---- maven qualifier ordering
    assert ("affected", "GHSA-EXAMPLE-3002") in status_of("maven", "legacy-logging", "1.0-alpha-3")

    # ---- unparseable version
    assert any(f["status"] == "invalid_version" and f["component"] == "tinycrypto"
               for f in findings)

    # ---- missing version -> unknown
    assert any(f["status"] == "missing_version" and f["component"] == "mystery-lib"
               for f in findings)

    # ---- package not in fixture -> explicitly no data, never "clean"
    assert any(f["status"] == "no_fixture_data" and f["component"] == "no-such-package"
               for f in findings)

    # ---- malformed purl
    assert any(f["status"] == "invalid_purl" and f["component"] == "broken-coordinate"
               for f in findings)

    # ---- unsupported ecosystem
    assert any(f["status"] == "unsupported_ecosystem" and f["component"] == "bar"
               for f in findings)

    # ---- excluded component kept but not evaluated
    assert any(f["status"] == "excluded" and f["component"] == "left-pad-lite"
               and f["version"] == "1.3.0" for f in findings)


def test_transitive_evidence_paths(client):
    body = _post(client, SAMPLE).json()
    tiny = [f for f in body["findings"]
            if f["component"] == "tinycrypto" and f["version"] == "1.1.0"
            and f["status"] == "affected"]
    assert tiny, "transitive tinycrypto@1.1.0 must be found via left-pad-lite"
    paths = tiny[0]["evidence_paths"]
    assert paths
    # every path starts at the direct dependency lpl-140 and ends at tiny-110
    assert any(p[0] == "lpl-140" and p[-1] == "tiny-110" for p in paths)
    assert tiny[0]["relation"] == "transitive"

    legacy = [f for f in body["findings"]
              if f["component"] == "legacy-logging" and f["status"] == "affected"][0]
    assert legacy["relation"] == "transitive"
    assert any("cc-140" in p for p in legacy["evidence_paths"])


def test_direct_relation_and_self_evidence(client):
    body = _post(client, SAMPLE).json()
    direct = [f for f in body["findings"]
              if f["component"] == "common-core" and f["version"] == "1.4.0"][0]
    assert direct["relation"] == "direct"
    assert direct["evidence_paths"] == [["cc-140"]]


def test_duplicate_components_merge_but_variants_do_not(client):
    body = _post(client, SAMPLE).json()
    comps = body["components"]
    # two bom-refs for the same purl@version@scope merge into one node
    dups = [c for c in comps if c["purl"] == "pkg:npm/duplicated-dep@1.0.0"]
    assert len(dups) == 1
    assert dups[0]["merged_count"] == 2
    assert set(dups[0]["bom_refs"]) == {"dup-1", "dup-2"}

    # qualifiers distinguish variants
    variants = [c for c in comps
                if (c["purl"] or "").startswith("pkg:npm/caret-lib@0.2.5")]
    assert len(variants) == 2
    assert {c["variant"]["qualifiers"].get("arch") for c in variants} == {"x64", "arm64"}
    assert body["summary"]["merged_duplicates"] >= 1


def test_dependency_cycles_reported_and_scan_terminates(client):
    body = _post(client, SAMPLE).json()
    assert body["summary"]["dependency_cycles"] >= 1
    cycles = body["dependency_cycles"]
    assert any(set(["loop-a", "loop-b"]).issubset(set(c)) for c in cycles)


def test_scan_persistence_roundtrip(client):
    body = _post(client, SAMPLE).json()
    scan_id = body["scan_id"]
    again = client.get(f"/api/v1/scans/{scan_id}")
    assert again.status_code == 200
    assert again.json()["input_sha256"] == body["input_sha256"]
    listed = client.get("/api/v1/scans").json()["scans"]
    assert any(s["scan_id"] == scan_id for s in listed)
    missing = client.get("/api/v1/scans/does-not-exist")
    assert missing.status_code == 404


def test_input_sha256_is_real_and_stable(client):
    raw = SAMPLE.read_bytes()
    import hashlib
    expected = hashlib.sha256(raw).hexdigest()
    body = _post(client, SAMPLE).json()
    assert body["input_sha256"] == expected
    assert len(expected) == 64


def test_rejects_non_cyclonedx(client):
    r = client.post("/api/v1/sboms/analyze",
                    content=json.dumps({"bomFormat": "SPDX", "specVersion": "2.3"}),
                    headers={"content-type": "application/json"})
    assert r.status_code == 400
    assert r.json()["error"] == "invalid_cyclonedx"


def test_rejects_bad_json(client):
    r = client.post("/api/v1/sboms/analyze", content=b"{not json")
    assert r.status_code == 400
    assert r.json()["error"] == "invalid_json"


def test_rejects_unknown_scope(client):
    doc = json.loads(SAMPLE.read_text())
    doc["components"][0]["scope"] = "runtime"
    r = client.post("/api/v1/sboms/analyze", content=json.dumps(doc),
                    headers={"content-type": "application/json"})
    assert r.status_code == 400
