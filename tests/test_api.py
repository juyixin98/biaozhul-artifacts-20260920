"""End-to-end FastAPI tests using the in-process TestClient."""

import json
import os
from pathlib import Path

import numpy as np
import pytest

# Point the server key at a temp location BEFORE importing the app.
_TMP_KEY_DIR = Path(__file__).resolve().parent / "_tmp_keys"
_TMP_KEY_DIR.mkdir(exist_ok=True)
os.environ["CALIB_AUDIT_KEY_PATH"] = str(_TMP_KEY_DIR / "test_key.pem")

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402

EXAMPLES = Path(__file__).resolve().parents[1] / "examples"


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


def _load(name):
    return json.loads((EXAMPLES / name).read_text())


class TestHealth:
    def test_health_reports_real_key(self, client):
        r = client.get("/health")
        assert r.status_code == 200
        body = r.json()
        assert body["status"] == "ok"
        assert len(bytes.fromhex(body["public_key_hex"])) == 32


class TestAuditEndpoint:
    def test_ok_loop_response_signed(self, client):
        r = client.post("/audit", json=_load("audit_basic.json"))
        assert r.status_code == 200
        b = r.json()
        assert b["summary"]["status"] == "ok"
        assert "signature" in b and "fingerprint_sha256" in b
        # Ed25519 signature is 64 bytes -> 128 hex chars
        assert len(b["signature"]) == 128

    def test_conflict_loop(self, client):
        r = client.post("/audit", json=_load("audit_conflict.json"))
        b = r.json()
        assert b["summary"]["status"] == "conflict"
        kinds = [f["kind"] for f in b["findings"]]
        assert "loop_closure_conflict" in kinds

    def test_illegal_input_rejected_not_500(self, client):
        r = client.post("/audit", json=_load("audit_illegal.json"))
        # parsed successfully as a request, audit reports fatal rejection
        assert r.status_code == 200
        b = r.json()
        assert b["fatal"] is True
        assert b["summary"]["status"] == "rejected"
        kinds = {f["kind"] for f in b["findings"]}
        assert "illegal_rotation" in kinds
        assert "covariance_not_psd" in kinds

    def test_missing_correlation_policy_is_422(self, client):
        payload = _load("audit_basic.json")
        del payload["correlation_policy"]
        r = client.post("/audit", json=payload)
        assert r.status_code == 422

    def test_mixed_convention_rejected(self, client):
        payload = _load("audit_basic.json")
        payload["edges"][0]["convention"] = "left"
        r = client.post("/audit", json=payload)
        b = r.json()
        assert b["fatal"] is True
        assert any(f["kind"] == "mixed_convention" for f in b["findings"])


class TestChainEndpoint:
    def test_open_chain(self, client):
        r = client.post("/chain", json=_load("chain_open.json"))
        assert r.status_code == 200
        b = r.json()
        assert b["summary"]["status"] == "propagated"
        assert np.array(b["covariance"]).shape == (6, 6)

    def test_missing_covariance_indeterminate(self, client):
        r = client.post("/audit", json=_load("audit_missing_covariance.json"))
        b = r.json()
        # graph is a tree with no closed loop, but no crash; status not conflict
        assert b["fatal"] is False


class TestMonteCarloEndpoint:
    def test_montecarlo_small_chain(self, client):
        payload = _load("montecarlo_long_chain.json")
        payload["n_samples"] = 4000  # keep the API test fast
        r = client.post("/montecarlo", json=payload)
        assert r.status_code == 200
        b = r.json()
        assert b["status"] == "ok"
        # 12-edge long chain at small noise stays a good approximation
        assert b["relative_frobenius_error"] < 0.08
        assert 0.90 < b["fraction_inside_chi2_95_ellipse"] < 0.99


class TestSignVerify:
    def test_sign_then_verify(self, client):
        payload = {"calibration_version": "v1", "data": [1, 2, 3]}
        s = client.post("/sign", json={"payload": payload}).json()
        pub = client.get("/health").json()["public_key_hex"]
        r = client.post(
            "/verify",
            json={"payload": payload, "signature": s["signature"], "public_key": pub},
        ).json()
        assert r["valid"] is True

    def test_verify_tampered_payload_fails(self, client):
        payload = {"calibration_version": "v1", "data": [1, 2, 3]}
        s = client.post("/sign", json={"payload": payload}).json()
        pub = client.get("/health").json()["public_key_hex"]
        r = client.post(
            "/verify",
            json={
                "payload": {**payload, "data": [1, 2, 999]},
                "signature": s["signature"],
                "public_key": pub,
            },
        ).json()
        assert r["valid"] is False

    def test_ingest_accepts_signed_bundle(self, client):
        from cryptography.hazmat.primitives.serialization import (
            Encoding,
            PublicFormat,
        )

        from app import crypto

        # sign with the server's own key via /sign then ingest
        payload = {"calibration_version": "bundle-v1", "edges": []}
        s = client.post("/sign", json={"payload": payload}).json()
        pub = client.get("/health").json()["public_key_hex"]
        r = client.post(
            "/calibration/ingest",
            json={"payload": payload, "signature": s["signature"], "public_key": pub},
        ).json()
        assert r["accepted"] is True
        assert r["signature_valid"] is True
        assert r["canonical_sha256"] == s["canonical_sha256"]
