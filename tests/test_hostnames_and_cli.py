"""Unit tests for hostname matching, PEM parsing and the CLI entrypoint."""

from __future__ import annotations

import ipaddress
import json
import subprocess
import sys
from pathlib import Path

import pytest

from cryptography import x509
from cryptography.hazmat.primitives import serialization
from cryptography.x509.oid import NameOID

from certverifier.hostnames import check_hostname
from certverifier.pemutils import parse_pem_certificates

ROOT = Path(__file__).resolve().parents[1]


def _leaf_with_san(dns=(), ips=(), cn=None):
    from certverifier.fixtures import make_root, issue, LEAF_START, LEAF_END
    root, rk = make_root("t")
    leaf, _ = issue(
        root, rk, cn or (dns[0] if dns else "leaf"),
        dns_names=tuple(dns), ip_addresses=tuple(ips),
        not_before=LEAF_START, not_after=LEAF_END,
    )
    return leaf


def test_dns_exact_and_case_insensitive():
    leaf = _leaf_with_san(dns=["Example.TEST"])
    assert check_hostname(leaf, "example.test") is None
    assert check_hostname(leaf, "EXAMPLE.test") is None


def test_wildcard_one_label_only():
    leaf = _leaf_with_san(dns=["*.example.com"])
    assert check_hostname(leaf, "a.example.com") is None
    assert check_hostname(leaf, "example.com") == "HOSTNAME_MISMATCH"
    assert check_hostname(leaf, "a.b.example.com") == "HOSTNAME_MISMATCH"


def test_ip_san():
    leaf = _leaf_with_san(dns=["x.test"], ips=["127.0.0.1", "::1"])
    assert check_hostname(leaf, "127.0.0.1") is None
    assert check_hostname(leaf, "::1") is None
    assert check_hostname(leaf, "10.0.0.1") == "HOSTNAME_MISMATCH"


def test_no_san_strict_rejects_cn():
    leaf = _leaf_with_san(dns=[], cn="cn-only.test")
    assert check_hostname(leaf, "cn-only.test") == "HOSTNAME_NO_SAN"
    assert check_hostname(leaf, "cn-only.test", allow_cn_fallback=True) is None


def test_pem_roundtrip_and_multi_block(scenarios):
    sc = scenarios["good"]
    text = sc.leaf.public_bytes(serialization.Encoding.PEM).decode()
    [parsed] = parse_pem_certificates(text)
    assert parsed.subject == sc.leaf.subject
    multi = text + text
    assert len(parse_pem_certificates(multi)) == 2
    with pytest.raises(ValueError):
        parse_pem_certificates("not a certificate")


def test_cli_good_and_expied(tmp_path, scenarios):
    def pem(c):
        return c.public_bytes(serialization.Encoding.PEM).decode()

    for name, expected_rc in (("good", 0), ("expired", 1),
                              ("path_length_violation", 1),
                              ("non_ca_intermediate", 1),
                              ("same_name_rogue_root", 1)):
        sc = scenarios[name]
        d = tmp_path / name
        d.mkdir()
        (d / "leaf.pem").write_text(pem(sc.leaf))
        (d / "int.pem").write_text("".join(pem(c) for c in sc.all_intermediates))
        (d / "root.pem").write_text("".join(pem(c) for c in sc.anchors))
        proc = subprocess.run(
            [
                sys.executable, "-m", "certverifier.cli",
                "--leaf", str(d / "leaf.pem"),
                "--intermediates", str(d / "int.pem"),
                "--trust-anchor", str(d / "root.pem"),
                "--verification-time",
                sc.verification_time.isoformat().replace("+00:00", "Z"),
                "--purpose", sc.purpose,
                "--hostname", sc.hostname or "unused.test",
            ],
            cwd=str(ROOT), capture_output=True, text=True,
        )
        assert proc.returncode == expected_rc, (name, proc.stdout, proc.stderr)
        body = json.loads(proc.stdout)
        assert body["valid"] is (expected_rc == 0)
        assert body["revocation"]["checked"] is False
