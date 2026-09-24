"""End-to-end HTTP tests using FastAPI's in-process TestClient."""

from __future__ import annotations

import datetime as dt

import pytest

from app.main import app
from fastapi.testclient import TestClient

from .certfactory import pem


@pytest.fixture(scope="module")
def client():
    return TestClient(app)


def _body(pki, leaf, *, inters=None, roots=None, subject="example.com", **extra):
    payload = {
        "leaf_certificate": pem(leaf.cert),
        "intermediate_certificates": [pem(c.cert if hasattr(c, "cert") else c)
                                      for c in (inters if inters is not None else [pki["intermediate"]])],
        "trust_roots": [pem(c.cert if hasattr(c, "cert") else c)
                        for c in (roots if roots is not None else [pki["root"]])],
        "purpose": "server_auth",
        "subject": subject,
    }
    payload.update(extra)
    return payload


def test_health(client):
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


def test_valid_chain_http(client, pki):
    r = client.post("/v1/verify", json=_body(pki, pki["leaf"]))
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["valid"] is True
    assert [c["role"] for c in data["chain"]] == ["leaf", "intermediate", "root"]
    assert data["error_code"] is None


def test_expired_leaf_http(client, pki, expired_leaf):
    r = client.post("/v1/verify", json=_body(
        pki, expired_leaf, subject="expired.example.com"))
    data = r.json()
    assert r.status_code == 200
    assert data["valid"] is False
    assert data["error_code"] == "VALIDITY_PERIOD"


def test_non_ca_intermediate_http(client, pki, non_ca_intermediate_chain):
    non_ca, leaf = non_ca_intermediate_chain
    r = client.post("/v1/verify", json=_body(
        pki, leaf, inters=[non_ca], subject="under-nonca.example.com"))
    data = r.json()
    assert data["valid"] is False
    assert data["error_code"] in ("NOT_A_CA", "REQUIRED_EXTENSION")


def test_name_mismatch_http(client, pki, name_mismatch_leaf):
    r = client.post("/v1/verify", json=_body(
        pki, name_mismatch_leaf, subject="example.com"))
    data = r.json()
    assert data["valid"] is False
    assert data["error_code"] == "NAME_MISMATCH"


def test_untrusted_self_signed_http(client, pki, untrusted_self_signed):
    r = client.post("/v1/verify", json=_body(
        pki, untrusted_self_signed, inters=[], subject="selfsigned.example.com"))
    data = r.json()
    assert data["valid"] is False
    assert data["error_code"] == "UNTRUSTED_CHAIN"


def test_attacker_root_as_intermediate_http(client, pki, untrusted_root_chain):
    bad_root, bad_inter, bad_leaf = untrusted_root_chain
    r = client.post("/v1/verify", json=_body(
        pki, bad_leaf, inters=[bad_inter, bad_root],
        subject="attacker.example.com"))
    data = r.json()
    assert data["valid"] is False
    assert data["error_code"] in ("UNTRUSTED_CHAIN", "MAX_CHAIN_DEPTH")


def test_pathlen_violation_http(client, pathlen_chains):
    (root0, inter0, leaf0), _ = pathlen_chains
    r = client.post("/v1/verify", json=_body(
        {"root": root0}, leaf0, inters=[inter0], roots=[root0],
        subject="p0.example.com"))
    data = r.json()
    assert data["valid"] is False
    assert data["error_code"] == "PATH_LEN_CONSTRAINT"


def test_client_purpose_http(client, pki):
    body = _body(pki, pki["client_leaf"], subject=None)
    body["purpose"] = "client_auth"
    del body["subject"]
    r = client.post("/v1/verify", json=body)
    assert r.status_code == 200, r.text
    assert r.json()["valid"] is True


def test_verification_time_override_http(client, pki, expired_leaf, now):
    past = (now - dt.timedelta(days=30)).isoformat()
    r = client.post("/v1/verify", json=_body(
        pki, expired_leaf, subject="expired.example.com",
        verification_time=past))
    assert r.json()["valid"] is True


def test_der_input_http(client, pki):
    from cryptography.hazmat.primitives.serialization import Encoding
    leaf_der = pki["leaf"].cert.public_bytes(Encoding.DER)
    import base64
    body = _body(pki, pki["leaf"])
    body["leaf_certificate"] = base64.b64encode(leaf_der).decode()
    r = client.post("/v1/verify", json=body)
    assert r.status_code == 200
    assert r.json()["valid"] is True


def test_pem_bundle_intermediates_http(client, pki):
    bundle = pem(pki["intermediate"].cert) + pem(pki["intermediate"].cert)
    body = _body(pki, pki["leaf"], inters=[])
    body["intermediate_certificates"] = [bundle]
    r = client.post("/v1/verify", json=body)
    assert r.json()["valid"] is True


def test_garbage_cert_returns_422(client, pki):
    body = _body(pki, pki["leaf"])
    body["leaf_certificate"] = "not-a-certificate"
    r = client.post("/v1/verify", json=body)
    assert r.status_code == 422
    assert r.json()["error"] == "certificate_parse_error"


def test_empty_trust_roots_422(client, pki):
    body = _body(pki, pki["leaf"])
    body["trust_roots"] = []
    r = client.post("/v1/verify", json=body)
    assert r.status_code == 422  # pydantic min_length


def test_missing_subject_for_server_400(client, pki):
    body = _body(pki, pki["leaf"])
    del body["subject"]
    r = client.post("/v1/verify", json=body)
    assert r.status_code == 400
    assert r.json()["error"] == "invalid_request"


def test_naive_time_400(client, pki):
    r = client.post("/v1/verify", json=_body(
        pki, pki["leaf"], verification_time="2026-09-24T12:00:00"))
    assert r.status_code == 400


def test_response_shape(client, pki):
    r = client.post("/v1/verify", json=_body(pki, pki["leaf"]))
    data = r.json()
    for key in ("valid", "purpose", "subject", "verification_time",
                "max_chain_depth", "error_code", "error_message",
                "chain", "findings"):
        assert key in data
    leaf = data["chain"][0]
    for key in ("role", "subject", "issuer", "serial_number", "not_before",
                "not_after", "is_ca", "san_dns_names", "eku",
                "fingerprint_sha256"):
        assert key in leaf
