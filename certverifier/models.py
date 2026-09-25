"""Data types shared by chain building and path validation."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Optional


class Purpose(str, Enum):
    """Intended key usage for the end-entity certificate (RFC 5280 4.2.1.12)."""

    SERVER_AUTH = "serverAuth"  # id-kp-serverAuth 1.3.6.1.5.5.7.3.1
    CLIENT_AUTH = "clientAuth"  # id-kp-clientAuth 1.3.6.1.5.5.7.3.2
    ANY = "any"

    @classmethod
    def parse(cls, value: str | Purpose) -> "Purpose":
        if isinstance(value, Purpose):
            return value
        normalized = str(value).strip()
        for member in cls:
            if member.value == normalized:
                return member
        raise ValueError(f"unsupported purpose {normalized!r}")


# Extended Key Usage OIDs (cannot reference x509 OID objects here without a
# circular import cost; strings are compared against oid.dotted_string).
EKU_OID = {
    Purpose.SERVER_AUTH: "1.3.6.1.5.5.7.3.1",
    Purpose.CLIENT_AUTH: "1.3.6.1.5.5.7.3.2",
}


@dataclass(frozen=True)
class VerificationOptions:
    """Toggles for validation checks that sites reasonably disagree on."""

    # When False (default) trust-anchor validity dates are not enforced:
    # anchors are trusted by provision, not by their notBefore/notAfter.
    check_anchor_validity: bool = False
    # RFC 6125 says CN fallback is deprecated; off by default.
    allow_cn_hostname: bool = False
    # SHA-1 / MD5 signatures are rejected unless this is set.
    allow_weak_signature_algorithms: bool = False


@dataclass
class Finding:
    """A single validation error."""

    code: str
    message: str
    certificate_index: Optional[int] = None
    subject: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": self.message,
            "certificate_index": self.certificate_index,
            "subject": self.subject,
        }


@dataclass
class VerificationResult:
    valid: bool
    findings: list[Finding] = field(default_factory=list)
    chain: list[dict[str, Any]] = field(default_factory=list)
    trust_anchor: Optional[dict[str, Any]] = None
    verification_time: Optional[datetime] = None
    purpose: Optional[str] = None
    hostname: Optional[str] = None
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        # Revocation reporting is deliberately present on every result so a
        # consumer can never mistake silence for a successful revocation check.
        revocation = {
            "checked": False,
            "crl_checked": False,
            "ocsp_checked": False,
            "note": (
                "OFFLINE MODE: revocation status (CRL/OCSP/OCSP-stapling) was "
                "NOT checked. A VALID result says nothing about whether any "
                "certificate in the chain has been revoked."
            ),
        }
        return {
            "valid": self.valid,
            "findings": [f.to_dict() for f in self.findings],
            "chain": self.chain,
            "trust_anchor": self.trust_anchor,
            "verification_time": (
                self.verification_time.astimezone(timezone.utc).isoformat()
                if self.verification_time
                else None
            ),
            "purpose": self.purpose,
            "hostname": self.hostname,
            "revocation": revocation,
            "notes": self.notes,
        }
