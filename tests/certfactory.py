"""Tiny local CA / certificate factory used by tests and by ``scripts/gen_certs.py``.

This is deliberately a minimal X.509 builder producing WebPKI-style
certificates (BasicConstraints + KeyUsage + SKI/AKI + optional SAN/EKU) so the
verification engine accepts well-formed chains and we can deliberately break
individual properties to produce negative-test fixtures.

Key types default to ECDSA P-256 for compactness; RSA is also supported.
"""

from __future__ import annotations

import datetime as dt
import ipaddress
from dataclasses import dataclass
from typing import Sequence

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, rsa
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

DAY = dt.timedelta(days=1)

SERVER_AUTH = ExtendedKeyUsageOID.SERVER_AUTH
CLIENT_AUTH = ExtendedKeyUsageOID.CLIENT_AUTH


def generate_key(key_type: str = "ec"):
    if key_type == "ec":
        return ec.generate_private_key(ec.SECP256R1())
    if key_type == "rsa":
        return rsa.generate_private_key(public_exponent=65537, key_size=2048)
    raise ValueError(f"unknown key_type {key_type!r}")


def pem(cert: x509.Certificate) -> str:
    return cert.public_bytes(serialization.Encoding.PEM).decode("ascii")


def key_pem(key) -> str:
    return key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.TraditionalOpenSSL,
        serialization.NoEncryption(),
    ).decode("ascii")


@dataclass
class IssuedCert:
    cert: x509.Certificate
    key: object
    name: str


def _name(cn: str) -> x509.Name:
    return x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, cn)])


def build_cert(
    cn: str,
    *,
    issuer: x509.Certificate | None,
    signing_key,
    subject_key=None,
    is_ca: bool,
    pathlen: int | None = None,
    dns_names: Sequence[str] = (),
    ip_names: Sequence[str] = (),
    eku: Sequence[object] | None = None,
    not_before: dt.datetime | None = None,
    not_after: dt.datetime | None = None,
    validity_days: int = 365,
    serial: int | None = None,
) -> x509.Certificate:
    """Build and sign an X.509 certificate.

    When *issuer* is None the certificate is self-signed. *subject_key* gives
    the holder's public key; when omitted the signing key's public key is used.
    """
    now = dt.datetime.now(dt.timezone.utc)
    nb = not_before or (now - DAY)
    na = not_after or (now + dt.timedelta(days=validity_days))
    holder_pub = (subject_key or signing_key).public_key()

    builder = (
        x509.CertificateBuilder()
        .subject_name(_name(cn))
        .issuer_name(issuer.subject if issuer is not None else _name(cn))
        .public_key(holder_pub)
        .serial_number(serial or x509.random_serial_number())
        .not_valid_before(nb)
        .not_valid_after(na)
        .add_extension(
            x509.BasicConstraints(ca=is_ca, path_length=pathlen), critical=True
        )
        .add_extension(
            x509.SubjectKeyIdentifier.from_public_key(holder_pub), critical=False
        )
        .add_extension(
            x509.AuthorityKeyIdentifier.from_issuer_public_key(
                signing_key.public_key()
            ),
            critical=False,
        )
        .add_extension(
            x509.KeyUsage(
                digital_signature=not is_ca,
                content_commitment=False,
                key_encipherment=False,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=is_ca,
                crl_sign=is_ca,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
    )

    general_names = [x509.DNSName(n) for n in dns_names]
    general_names += [
        x509.IPAddress(ipaddress.ip_address(ip)) for ip in ip_names
    ]
    if general_names:
        builder = builder.add_extension(
            x509.SubjectAlternativeName(general_names), critical=False
        )

    if eku is not None:
        builder = builder.add_extension(
            x509.ExtendedKeyUsage(list(eku)), critical=False
        )

    return builder.sign(signing_key, hashes.SHA256())


def make_root(cn: str = "Test Root CA", *, pathlen: int | None = None,
              not_before=None, not_after=None, validity_days: int = 3650,
              key_type: str = "ec") -> IssuedCert:
    k = generate_key(key_type)
    current = dt.datetime.now(dt.timezone.utc)
    c = build_cert(
        cn, issuer=None, signing_key=k, is_ca=True, pathlen=pathlen,
        not_before=not_before or (current - dt.timedelta(days=validity_days)),
        not_after=not_after, validity_days=validity_days,
    )
    return IssuedCert(c, k, cn)


def make_intermediate(
    root: IssuedCert,
    cn: str = "Test Intermediate CA",
    *,
    pathlen: int | None = None,
    is_ca: bool = True,
    not_before=None,
    not_after=None,
    validity_days: int = 1825,
    key_type: str = "ec",
) -> IssuedCert:
    k = generate_key(key_type)
    current = dt.datetime.now(dt.timezone.utc)
    c = build_cert(
        cn, issuer=root.cert, signing_key=root.key, subject_key=k,
        is_ca=is_ca, pathlen=pathlen,
        not_before=not_before or (current - dt.timedelta(days=validity_days)),
        not_after=not_after, validity_days=validity_days,
    )
    return IssuedCert(c, k, cn)


def make_leaf(
    issuer: IssuedCert,
    cn: str = "example.com",
    *,
    dns_names: Sequence[str] = ("example.com",),
    ip_names: Sequence[str] = (),
    eku: Sequence[object] = (SERVER_AUTH,),
    not_before=None,
    not_after=None,
    validity_days: int = 365,
    key_type: str = "ec",
) -> IssuedCert:
    k = generate_key(key_type)
    c = build_cert(
        cn, issuer=issuer.cert, signing_key=issuer.key, subject_key=k,
        is_ca=False, dns_names=dns_names, ip_names=ip_names, eku=list(eku),
        not_before=not_before, not_after=not_after, validity_days=validity_days,
    )
    return IssuedCert(c, k, cn)
