"""Authoritative offline X.509 chain verification.

The actual trust decision is delegated to
``cryptography.x509.verification.PolicyBuilder`` (cryptography >= 42), which is
a binding to the Rust webpki/rustls path-building and validation engine.
This module wraps it so the FastAPI layer works with plain data structures and
gets back a structured verdict plus diagnostic findings.

Security properties enforced by the authoritative verifier:

* cryptographic signature verification for every link of the chain;
* validity windows of the leaf, every intermediate and the trust anchor;
* BasicConstraints (CA bit) and pathLenConstraint;
* name chaining (issuer/subject, AKI/SKI) and signature algorithm support;
* RFC 5280 required extensions for a valid PKIX/WebPKI chain;
* ExtendedKeyUsage for the selected purpose (serverAuth/clientAuth);
* DNS/IP subject name matching against the leaf SAN.

There is deliberately **no** OCSP/CRL/network access anywhere: revocation is
out of scope for offline verification.
"""

from __future__ import annotations

import ipaddress
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from cryptography import x509
from cryptography.x509 import verification as webpki

from .pki import (
    ChainLink,
    build_diagnostic_chain,
    cert_summary,
    diagnose_eku,
    diagnose_pathlen,
    now_utc,
    san_matches,
    time_validity,
)

PURPOSE_SERVER = "server_auth"
PURPOSE_CLIENT = "client_auth"
VALID_PURPOSES = (PURPOSE_SERVER, PURPOSE_CLIENT)


class VerificationInputError(ValueError):
    """Caller supplied an unusable request (bad certs, bad parameters)."""


@dataclass
class VerificationResult:
    valid: bool
    purpose: str
    subject: str | None
    verification_time: str
    max_chain_depth: int | None
    error_code: str | None = None
    error_message: str | None = None
    chain: list[dict[str, Any]] = field(default_factory=list)
    findings: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "valid": self.valid,
            "purpose": self.purpose,
            "subject": self.subject,
            "verification_time": self.verification_time,
            "max_chain_depth": self.max_chain_depth,
            "error_code": self.error_code,
            "error_message": self.error_message,
            "chain": self.chain,
            "findings": self.findings,
        }


def _parse_verification_time(value: str | datetime | None) -> datetime:
    if value is None:
        return now_utc()
    if isinstance(value, datetime):
        dt = value
    else:
        try:
            dt = datetime.fromisoformat(value)
        except (TypeError, ValueError) as exc:
            raise VerificationInputError(
                f"verification_time must be an ISO 8601 timestamp: {exc}"
            ) from exc
    if dt.tzinfo is None:
        raise VerificationInputError(
            "verification_time must be timezone-aware (e.g. 2026-09-24T12:00:00Z)"
        )
    return dt


def _classify_error(message: str) -> str:
    """Map a webpki error string to a stable machine-readable error code."""
    m = message.lower()
    if "is not valid at validation time" in m:
        return "VALIDITY_PERIOD"
    if "no matching subjectaltname" in m:
        return "NAME_MISMATCH"
    if "has no subjectaltname" in m:
        return "NAME_MISMATCH"
    if "path length constraint violated" in m:
        return "PATH_LEN_CONSTRAINT"
    if "exceeds max depth" in m:
        return "MAX_CHAIN_DEPTH"
    if "ca must be asserted" in m or "basicconstraints" in m:
        return "NOT_A_CA"
    if "required eku not found" in m:
        return "EKU"
    if "missing required extension" in m or "invalid extension" in m:
        return "REQUIRED_EXTENSION"
    if "all candidates exhausted" in m or "candidates exhausted" in m:
        return "UNTRUSTED_CHAIN"
    return "VERIFICATION_FAILED"


def _links_to_dicts(links: list[ChainLink]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for link in links:
        entry = {"role": link.role, "issuer_found": link.issuer_found,
                 **cert_summary(link.cert)}
        if link.reason:
            entry["chain_note"] = link.reason
        out.append(entry)
    return out


def _diagnose(
    links: list[ChainLink],
    purpose: str,
    subject: str | None,
    at: datetime,
) -> list[dict[str, Any]]:
    """Collect independent findings (never overrides the authoritative verdict)."""
    findings: list[dict[str, Any]] = []

    for link in links:
        state = time_validity(link.cert, at)
        if state:
            findings.append({
                "code": "CERT_EXPIRED" if state == "expired" else "CERT_NOT_YET_VALID",
                "severity": "error",
                "cert": link.cert.subject.rfc4514_string(),
                "role": link.role,
                "not_before": link.cert.not_valid_before_utc.isoformat(),
                "not_after": link.cert.not_valid_after_utc.isoformat(),
                "detail": f"certificate is {state.replace('_', ' ')} at verification time",
            })

    leaf = links[0].cert
    if not links[0].issuer_found:
        findings.append({
            "code": "ISSUER_NOT_FOUND",
            "severity": "error",
            "cert": leaf.subject.rfc4514_string(),
            "detail": "no certificate matching the leaf issuer/AKI was supplied",
        })

    pathlen = diagnose_pathlen(links)
    if pathlen:
        code = pathlen.pop("code")
        findings.append({"code": code, "severity": "error", **pathlen})

    eku = diagnose_eku(leaf, purpose)
    if eku:
        findings.append({"code": "EKU_MISMATCH", "severity": "error", **eku})

    if subject is not None:
        if san_matches(leaf, subject):
            findings.append({
                "code": "NAME_MATCH",
                "severity": "info",
                "cert": leaf.subject.rfc4514_string(),
                "subject": subject,
            })
        else:
            findings.append({
                "code": "NAME_MISMATCH",
                "severity": "error",
                "cert": leaf.subject.rfc4514_string(),
                "subject": subject,
                "san_dns_names": cert_summary(leaf)["san_dns_names"],
                "san_ip_addresses": cert_summary(leaf)["san_ip_addresses"],
                "detail": "requested name is not present in the leaf SAN",
            })

    return findings


def verify_chain(
    leaf_cert: x509.Certificate,
    intermediates: list[x509.Certificate],
    trust_roots: list[x509.Certificate],
    *,
    purpose: str = PURPOSE_SERVER,
    subject: str | None = None,
    verification_time: str | datetime | None = None,
    max_chain_depth: int | None = None,
) -> VerificationResult:
    """Verify *leaf_cert* offline against explicit *trust_roots*.

    *intermediates* are untrusted candidate certificates used by the path
    builder; anything not actually needed is ignored.  A self-signed
    certificate in *intermediates* can never act as a trust anchor — anchors
    come exclusively from *trust_roots*.
    """
    if purpose not in VALID_PURPOSES:
        raise VerificationInputError(
            f"purpose must be one of {VALID_PURPOSES}, got {purpose!r}"
        )
    if not trust_roots:
        raise VerificationInputError(
            "at least one trust root is required; an empty trust store is not allowed"
        )
    if max_chain_depth is not None and max_chain_depth < 0:
        raise VerificationInputError("max_chain_depth must be >= 0")

    if purpose == PURPOSE_SERVER:
        if not subject:
            raise VerificationInputError(
                "subject (DNS name or IP address) is required for server_auth"
            )
        try:
            ip = ipaddress.ip_address(subject)
        except ValueError:
            webpki_subject = webpki.DNSName(subject)
        else:
            webpki_subject = webpki.IPAddress(ip)
    else:
        webpki_subject = None

    at = _parse_verification_time(verification_time)

    links = build_diagnostic_chain(leaf_cert, intermediates, trust_roots)
    findings = _diagnose(links, purpose, subject if purpose == PURPOSE_SERVER else None, at)
    chain_dicts = _links_to_dicts(links)

    try:
        store = webpki.Store(trust_roots)
        builder = webpki.PolicyBuilder().store(store).time(at)
        if max_chain_depth is not None:
            builder = builder.max_chain_depth(max_chain_depth)
        if purpose == PURPOSE_SERVER:
            verifier = builder.build_server_verifier(webpki_subject)
        else:
            verifier = builder.build_client_verifier()
        verified = verifier.verify(leaf_cert, intermediates)
    except webpki.VerificationError as exc:
        message = str(exc)
        result = VerificationResult(
            valid=False,
            purpose=purpose,
            subject=subject,
            verification_time=at.isoformat(),
            max_chain_depth=max_chain_depth,
            # strip the rust-side processing context for a clean top-level message
            error_code=_classify_error(message),
            error_message=message.split("(encountered processing")[0].strip(),
            chain=chain_dicts,
            findings=findings,
        )
        return result

    # Server verifier returns the ordered chain list directly; client verifier
    # returns a VerifiedClient with .chain / .subjects.
    verified_chain = getattr(verified, "chain", verified)
    verified_subjects = getattr(verified, "subjects", None)
    if verified_subjects is not None:
        findings.append({
            "code": "CLIENT_SUBJECTS",
            "severity": "info",
            "subjects": [str(s) for s in verified_subjects],
        })

    return VerificationResult(
        valid=True,
        purpose=purpose,
        subject=subject,
        verification_time=at.isoformat(),
        max_chain_depth=max_chain_depth,
        chain=chain_dicts,
        findings=findings + [{
            "code": "VERIFIED",
            "severity": "info",
            "detail": (
                f"chain built to trusted anchor; {len(verified_chain)} certificates "
                "passed signature, validity, name, EKU and constraints checks"
            ),
        }],
    )
