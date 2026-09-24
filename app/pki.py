"""Offline X.509 certificate handling helpers.

Everything here is *diagnostic* support code.  The authoritative chain
validation decision is made by ``cryptography.x509.verification`` (a binding
to the Rust ``webpki`` / rustls verification engine); see :mod:`app.verifier`.
These helpers only parse certificates and produce the human-readable findings
that accompany the authoritative verdict.
"""

from __future__ import annotations

import base64
import binascii
import ipaddress
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Iterable

from cryptography import x509
from cryptography.hazmat.primitives.hashes import SHA256
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID, ObjectIdentifier

_PEM_RE = re.compile(
    rb"-----BEGIN CERTIFICATE-----\s*(.*?)\s*-----END CERTIFICATE-----",
    re.DOTALL,
)

# EKU OIDs we report by name in findings.
_EKU_NAMES = {
    ExtendedKeyUsageOID.SERVER_AUTH: "serverAuth",
    ExtendedKeyUsageOID.CLIENT_AUTH: "clientAuth",
    ExtendedKeyUsageOID.CODE_SIGNING: "codeSigning",
    ExtendedKeyUsageOID.EMAIL_PROTECTION: "emailProtection",
    ExtendedKeyUsageOID.TIME_STAMPING: "timeStamping",
    ExtendedKeyUsageOID.OCSP_SIGNING: "OCSPSigning",
    ObjectIdentifier("2.5.29.37.0"): "anyExtendedKeyUsage",
}


class CertLoadError(ValueError):
    """Raised when a certificate string cannot be parsed as PEM or DER."""


def load_certificates(raw: str | bytes) -> list[x509.Certificate]:
    """Load one or more certificates from PEM (armour/base64) or DER.

    A PEM string may contain multiple ``CERTIFICATE`` blocks (a "bundle").
    A bare base64 body or DER blob yields a single certificate.
    """
    data = raw.encode("ascii", errors="strict") if isinstance(raw, str) else raw
    stripped = data.strip()
    if b"-----BEGIN CERTIFICATE-----" in stripped:
        blocks = [m.group(0) for m in _PEM_RE.finditer(stripped)]
        if not blocks:
            raise CertLoadError("malformed PEM: no complete CERTIFICATE block")
        certs: list[x509.Certificate] = []
        for block in blocks:
            try:
                certs.append(x509.load_pem_x509_certificate(block))
            except Exception as exc:
                raise CertLoadError(f"invalid PEM certificate: {exc}") from exc
        return certs
    single = load_certificate(stripped)
    return [single]


def load_certificate(raw: str | bytes) -> x509.Certificate:
    """Load one DER or PEM encoded X.509 certificate.

    PEM input may contain extra text around the certificate block, but it must
    contain exactly one ``BEGIN CERTIFICATE`` block.
    """
    if isinstance(raw, bytes):
        data = raw
    else:
        data = raw.encode("ascii", errors="strict")

    stripped = data.strip()
    if b"-----BEGIN CERTIFICATE-----" in stripped:
        matches = list(_PEM_RE.finditer(stripped))
        if len(matches) != 1:
            raise CertLoadError(
                f"expected exactly 1 PEM CERTIFICATE block, found {len(matches)}"
            )
        try:
            return x509.load_pem_x509_certificate(stripped)
        except Exception as exc:  # cryptography raises various ValueError subtypes
            raise CertLoadError(f"invalid PEM certificate: {exc}") from exc

    # Assume raw base64 PEM body without armour, or binary DER.
    try:
        return x509.load_der_x509_certificate(stripped)
    except Exception:
        pass
    try:
        der = base64.b64decode(stripped, validate=True)
        return x509.load_der_x509_certificate(der)
    except (binascii.Error, ValueError, TypeError) as exc:
        raise CertLoadError(
            "certificate is neither valid PEM nor valid DER (base64)"
        ) from exc


def _name_attr(name: x509.Name, oid: ObjectIdentifier) -> str | None:
    try:
        return name.get_attributes_for_oid(oid)[0].value
    except IndexError:
        return None


def cert_summary(cert: x509.Certificate) -> dict:
    """Return a JSON-serialisable summary used in API responses."""
    try:
        san_ext = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName)
        san = san_ext.value
        dns_names = list(san.get_values_for_type(x509.DNSName))
        ip_names = [str(ip) for ip in san.get_values_for_type(x509.IPAddress)]
    except x509.ExtensionNotFound:
        dns_names, ip_names = [], []

    try:
        bc = cert.extensions.get_extension_for_class(x509.BasicConstraints).value
        is_ca, path_len = bool(bc.ca), bc.path_length
    except x509.ExtensionNotFound:
        is_ca, path_len = False, None

    eku: list[str] = []
    try:
        eku_ext = cert.extensions.get_extension_for_class(x509.ExtendedKeyUsage).value
        eku = [_EKU_NAMES.get(o, o.dotted_string) for o in eku_ext]
    except x509.ExtensionNotFound:
        pass

    return {
        "subject_cn": _name_attr(cert.subject, NameOID.COMMON_NAME),
        "subject": cert.subject.rfc4514_string(),
        "issuer": cert.issuer.rfc4514_string(),
        "serial_number": format(cert.serial_number, "x"),
        "not_before": cert.not_valid_before_utc.isoformat(),
        "not_after": cert.not_valid_after_utc.isoformat(),
        "is_ca": is_ca,
        "path_length_constraint": path_len,
        "san_dns_names": dns_names,
        "san_ip_addresses": ip_names,
        "eku": eku,
        "fingerprint_sha256": cert.fingerprint(SHA256()).hex(),
    }


def time_validity(cert: x509.Certificate, at: datetime) -> str | None:
    """Return ``"expired"`` / ``"not_yet_valid"`` / ``None`` at time *at*."""
    if at > cert.not_valid_after_utc:
        return "expired"
    if at < cert.not_valid_before_utc:
        return "not_yet_valid"
    return None


def dns_name_matches(pattern: str, hostname: str) -> bool:
    """RFC 6125 wildcard matching: ``*`` may only be the entire left-most label.

    ``*.example.com`` matches ``foo.example.com`` but not ``example.com`` or
    ``a.b.example.com``.  Comparison is ASCII case-insensitive.
    """
    p = pattern.strip(".").lower()
    h = hostname.strip(".").lower()
    if "*" not in p:
        return p == h
    labels = p.split(".")
    if labels[0] != "*" or any("*" in label for label in labels[1:]):
        return False
    h_labels = h.split(".")
    if len(h_labels) != len(labels):
        return False
    # Wildcard must not match an empty or IDNA-leading-dot-only label.
    if not h_labels[0]:
        return False
    return h_labels[1:] == labels[1:]


def san_matches(cert: x509.Certificate, host: str) -> bool:
    """Check *host* (DNS name or IP literal) against the SAN extension."""
    try:
        san = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    except x509.ExtensionNotFound:
        return False

    # IP literal?
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        ip = None
    if ip is not None:
        return any(addr == ip for addr in san.get_values_for_type(x509.IPAddress))

    return any(dns_name_matches(n, host) for n in san.get_values_for_type(x509.DNSName))


@dataclass
class ChainLink:
    cert: x509.Certificate
    role: str  # "leaf" | "intermediate" | "root"
    issuer_found: bool = True
    reason: str | None = None


def _ski(cert: x509.Certificate) -> bytes | None:
    try:
        return cert.extensions.get_extension_for_class(x509.SubjectKeyIdentifier).value.digest
    except x509.ExtensionNotFound:
        return None


def _aki(cert: x509.Certificate) -> bytes | None:
    try:
        return cert.extensions.get_extension_for_class(
            x509.AuthorityKeyIdentifier
        ).value.key_identifier
    except x509.ExtensionNotFound:
        return None


def _signed_by(child: x509.Certificate, issuer: x509.Certificate) -> bool:
    """Cheap structural check: subject/issuer names and AKI/SKI line up.

    Cryptographic signature verification is performed by the authoritative
    verifier; this only reconstructs the presentation order of the chain.
    """
    if child.issuer != issuer.subject:
        return False
    aki, ski = _aki(child), _ski(issuer)
    if aki is not None and ski is not None and aki != ski:
        return False
    return True


def build_diagnostic_chain(
    leaf: x509.Certificate,
    intermediates: Iterable[x509.Certificate],
    roots: Iterable[x509.Certificate],
) -> list[ChainLink]:
    """Best-effort ordered walk leaf -> ... -> root for the findings report."""
    ints = list(intermediates)
    root_list = list(roots)
    links: list[ChainLink] = [ChainLink(leaf, "leaf")]
    current = leaf
    used: set[int] = set()

    while True:
        if _signed_by(current, current) and any(
            current is r
            or current.fingerprint(SHA256()) == r.fingerprint(SHA256())
            for r in root_list
        ):
            links[-1].role = "root"
            return links

        issuer_idx = next(
            (i for i, c in enumerate(ints)
             if i not in used and _signed_by(current, c)),
            None,
        )
        if issuer_idx is not None:
            used.add(issuer_idx)
            issuer = ints[issuer_idx]
            links.append(ChainLink(issuer, "intermediate"))
            current = issuer
            continue

        root = next((r for r in root_list if _signed_by(current, r)), None)
        if root is not None:
            links.append(ChainLink(root, "root"))
            return links

        links[-1].issuer_found = False
        links[-1].reason = "no issuer found among supplied intermediates or trust roots"
        return links


def diagnose_pathlen(links: list[ChainLink]) -> dict | None:
    """Check BasicConstraints.path_length for every CA in the built chain.

    RFC 5280: pathLenConstraint limits the number of non-self-issued
    intermediate CA certificates that may follow the CA.
    """
    cas = [l for l in links if l.role in ("intermediate", "root")]
    below = 0
    # walk from leaf upward: the intermediate directly above the leaf has
    # 1 CA below it, etc.  roots' own constraints don't constrain themselves.
    for link in reversed(links):
        if link.role == "intermediate":
            try:
                bc = link.cert.extensions.get_extension_for_class(
                    x509.BasicConstraints
                ).value
            except x509.ExtensionNotFound:
                return {"code": "NOT_A_CA",
                        "cert": link.cert.subject.rfc4514_string(),
                        "detail": "intermediate lacks basicConstraints (not a CA)"}
            if not bc.ca:
                return {"code": "NOT_A_CA",
                        "cert": link.cert.subject.rfc4514_string(),
                        "detail": "basicConstraints.cA not asserted on intermediate"}
            if bc.path_length is not None and below > bc.path_length:
                return {
                    "code": "PATH_LEN_VIOLATION",
                    "cert": link.cert.subject.rfc4514_string(),
                    "path_length_constraint": bc.path_length,
                    "intermediate_cas_below": below,
                    "detail": "path length constraint violated",
                }
            below += 1
    return None


def diagnose_eku(cert: x509.Certificate, purpose: str) -> dict | None:
    """Check leaf EKU for the requested purpose (informational)."""
    wanted = {
        "server_auth": ExtendedKeyUsageOID.SERVER_AUTH,
        "client_auth": ExtendedKeyUsageOID.CLIENT_AUTH,
    }.get(purpose)
    if wanted is None:
        return None
    try:
        ext = cert.extensions.get_extension_for_class(x509.ExtendedKeyUsage).value
    except x509.ExtensionNotFound:
        # Absent EKU means "any" per CA/Browser conventions.
        return None
    if wanted in ext or ExtendedKeyUsageOID.ANY_EXTENDED_KEY_USAGE in ext:
        return None
    return {
        "cert": cert.subject.rfc4514_string(),
        "required_eku": _EKU_NAMES.get(wanted, wanted.dotted_string),
        "present_eku": [_EKU_NAMES.get(o, o.dotted_string) for o in ext],
        "detail": "required EKU not present",
    }


def now_utc() -> datetime:
    return datetime.now(timezone.utc)
