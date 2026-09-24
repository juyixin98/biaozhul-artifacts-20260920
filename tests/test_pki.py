"""Unit tests for PEM/DER parsing and DNS wildcard matching helpers."""

from __future__ import annotations

import base64

import pytest
from cryptography.hazmat.primitives.serialization import Encoding
from cryptography import x509

from app.pki import (
    CertLoadError,
    dns_name_matches,
    load_certificate,
    load_certificates,
    san_matches,
)

from .certfactory import pem


def test_load_pem(pki):
    c = load_certificate(pem(pki["leaf"].cert))
    assert isinstance(c, x509.Certificate)


def test_load_der_binary_and_base64(pki):
    der = pki["leaf"].cert.public_bytes(Encoding.DER)
    assert load_certificate(der).subject == pki["leaf"].cert.subject
    assert load_certificate(base64.b64encode(der)).subject == pki["leaf"].cert.subject


def test_load_pem_with_surrounding_whitespace(pki):
    text = "\n\n  " + pem(pki["leaf"].cert) + "\n"
    assert load_certificate(text).subject == pki["leaf"].cert.subject


def test_load_rejects_garbage():
    with pytest.raises(CertLoadError):
        load_certificate("-----BEGIN CERTIFICATE-----\nnotbase64!!\n-----END CERTIFICATE-----")
    with pytest.raises(CertLoadError):
        load_certificate("hello world not a cert")


def test_load_multiple_pem_blocks(pki):
    bundle = pem(pki["leaf"].cert) + pem(pki["intermediate"].cert)
    certs = load_certificates(bundle)
    assert [c.subject.rfc4514_string() for c in certs] == [
        pki["leaf"].cert.subject.rfc4514_string(),
        pki["intermediate"].cert.subject.rfc4514_string(),
    ]


def test_load_single_block_via_bundle(pki):
    assert len(load_certificates(pem(pki["root"].cert))) == 1


def test_two_blocks_where_one_garbage_rejected(pki):
    bad = "-----BEGIN CERTIFICATE-----\n@@@@@\n-----END CERTIFICATE-----"
    with pytest.raises(CertLoadError):
        load_certificates(pem(pki["leaf"].cert) + bad)


@pytest.mark.parametrize(
    "pattern,host,expected",
    [
        ("example.com", "example.com", True),
        ("example.com", "EXAMPLE.com", True),
        ("example.com.", "example.com", True),
        ("*.example.com", "foo.example.com", True),
        ("*.example.com", "example.com", False),
        ("*.example.com", "a.b.example.com", False),
        ("foo.*.com", "foo.bar.com", False),
        ("a*.example.com", "ab.example.com", False),
        ("*.example.com", "other.com", False),
    ],
)
def test_dns_wildcard_rules(pattern, host, expected):
    assert dns_name_matches(pattern, host) is expected


def test_san_matches_ip(pki, ip_chain):
    assert san_matches(ip_chain.cert, "10.20.30.40")
    assert not san_matches(ip_chain.cert, "10.20.30.41")
    assert not san_matches(ip_chain.cert, "example.com")


def test_san_matches_dns(pki):
    assert san_matches(pki["leaf"].cert, "example.com")
    assert san_matches(pki["leaf"].cert, "www.example.com")
    assert not san_matches(pki["leaf"].cert, "evil-example.com")
