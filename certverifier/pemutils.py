"""PEM parsing helpers and certificate summaries."""

from __future__ import annotations

import base64
import re
from typing import Iterable

from cryptography import x509
from cryptography.hazmat.primitives import hashes

_PEM_RE = re.compile(
    rb"-----BEGIN CERTIFICATE-----[ \t]*\r?\n"
    rb"(?P<body>[A-Za-z0-9+/=\r\n \t]+?)"
    rb"-----END CERTIFICATE-----[ \t]*",
    re.DOTALL,
)


def parse_pem_certificates(data: str | bytes) -> list[x509.Certificate]:
    """Parse one or more concatenated PEM CERTIFICATE blocks.

    DER, PKCS#7 and other container formats are intentionally rejected: the
    service contract is PEM only.
    """
    raw = data.encode("ascii") if isinstance(data, str) else bytes(data)
    certs: list[x509.Certificate] = []
    for match in _PEM_RE.finditer(raw):
        body = re.sub(rb"\s+", b"", match.group("body"))
        try:
            der = base64.b64decode(body, validate=True)
            certs.append(x509.load_der_x509_certificate(der))
        except (ValueError, base64.binascii.Error) as exc:
            raise ValueError(f"malformed PEM certificate block: {exc}") from exc
    if not certs:
        raise ValueError(
            "no PEM CERTIFICATE blocks found (expected "
            "'-----BEGIN CERTIFICATE-----')"
        )
    return certs


def certificate_fingerprint(cert: x509.Certificate) -> bytes:
    return cert.fingerprint(hashes.SHA256())


def _not_before(cert: x509.Certificate):
    # cryptography >= 42 exposes tz-aware *_utc properties; on older versions
    # not_valid_before is a naive UTC datetime.
    value = getattr(cert, "not_valid_before_utc", None)
    if value is not None:
        return value
    return cert.not_valid_before.replace(tzinfo=__import__("datetime").timezone.utc)


def _not_after(cert: x509.Certificate):
    value = getattr(cert, "not_valid_after_utc", None)
    if value is not None:
        return value
    return cert.not_valid_after.replace(tzinfo=__import__("datetime").timezone.utc)


def certificate_summary(cert: x509.Certificate, *, index=None, is_anchor=False) -> dict:
    """JSON-serialisable view of a certificate for results and logs."""
    try:
        ski = cert.extensions.get_extension_for_class(
            x509.SubjectKeyIdentifier
        ).value.digest.hex()
    except x509.ExtensionNotFound:
        ski = None
    return {
        "index": index,
        "subject": cert.subject.rfc4514_string(),
        "issuer": cert.issuer.rfc4514_string(),
        "serial_number": format(cert.serial_number, "x"),
        "not_before": _not_before(cert).isoformat(),
        "not_after": _not_after(cert).isoformat(),
        "signature_algorithm": cert.signature_algorithm_oid._name,
        "sha256_fingerprint": certificate_fingerprint(cert).hex(":"),
        "subject_key_identifier": ski,
        "is_trust_anchor": is_anchor,
    }


def dedupe(certs: Iterable[x509.Certificate]) -> list[x509.Certificate]:
    """Drop duplicate certificates (by DER content), preserving order."""
    seen: set[bytes] = set()
    out: list[x509.Certificate] = []
    for cert in certs:
        key = cert.public_bytes(__import__(
            "cryptography.hazmat.primitives.serialization", fromlist=["Encoding"]
        ).Encoding.DER)
        if key not in seen:
            seen.add(key)
            out.append(cert)
    return out
