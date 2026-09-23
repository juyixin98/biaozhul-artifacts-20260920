"""Typed result models and the three verdict levels."""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, Field


class Verdict(str, Enum):
    EXACT = "EXACT_MATCH"
    RULE = "RULE_MATCH"
    MISMATCH = "MISMATCH"


class Severity(str, Enum):
    # Structural/integrity facts
    FACT = "FACT"
    # Allowed, rule-bound deviation
    ALLOWED = "ALLOWED"
    # Transparency note; never hides a difference
    WARNING = "WARNING"
    # A real, non-allowable difference
    ERROR = "ERROR"


# Finding codes -> default severity and whether an ALLOWED deviation
FINDING_CODES: dict[str, tuple[Severity, str]] = {
    "PACKAGE_ACCEPTED": (Severity.FACT, "uploaded package passed containment checks"),
    "SCRIPT_NOT_EXECUTED": (Severity.FACT, "executable members were read as data only"),
    "PATH_OK": (Severity.FACT, "all referenced source paths are contained under the package root"),
    "SOURCE_DIGEST_OK": (Severity.FACT, "recomputed source digests equal the compiler-output digests"),
    "METADATA_TAIL_PARSED": (Severity.FACT, "CBOR auxdata tail decoded"),
    "METADATA_HASH_OK": (Severity.FACT, "recomputed metadata hash equals the auxdata commitment"),
    "PLACEHOLDER_SCHEME_OK": (Severity.FACT, "library placeholder commits to the referenced FQN"),
    "LINK_REF_OK": (Severity.FACT, "declared linkReference offsets align with the placeholder slot"),
    "CONFIG_OK": (Severity.FACT, "submitted configuration matches the compiler-output metadata"),
    "VERSION_OK": (Severity.FACT, "compiler version matches exactly"),
    "ADDRESS_CHECKSUM_OK": (Severity.FACT, "library address is valid EIP-55 checksummed"),
    "BODY_OK": (Severity.FACT, "runtime bodies match after only allowed normalization"),
    "NO_RUNTIME_COMPARISON": (Severity.FACT, "no deployed runtime code supplied; integrity checks only"),

    "LIBRARY_ADDRESS_DIFF": (Severity.ALLOWED, "only linked library addresses differ; placeholder slots align"),
    "METADATA_TAIL_DIFF_HASH": (Severity.ALLOWED, "metadata content hash differs (embedded content-addressed hash)"),

    "UNSAFE_PATH": (Severity.ERROR, "path traversal, absolute path or special member in package"),
    "SOURCE_MISSING": (Severity.ERROR, "a source referenced by the compiler output is absent from the package"),
    "SOURCE_DIGEST_DIFF": (Severity.ERROR, "recomputed source digest differs from compiler output (semantic change)"),
    "SOURCE_EXTRA": (Severity.WARNING, "package contains files not referenced by the compiler output"),
    "VERSION_DIFF": (Severity.ERROR, "compiler version mismatch"),
    "SETTING_DIFF": (Severity.ERROR, "optimizer/evm setting differs from recorded compilation settings"),
    "METADATA_TAIL_MALFORMED": (Severity.ERROR, "auxdata tail missing or CBOR does not decode"),
    "METADATA_HASH_DIFF": (Severity.ERROR, "recomputed metadata hash does not match the auxdata commitment"),
    "METADATA_TAIL_DIFF_OTHER": (Severity.ERROR, "auxdata fields other than content hash differ"),
    "BODY_DIFF": (Severity.ERROR, "runtime code differs outside metadata and library-address slots"),
    "PLACEHOLDER_UNRESOLVED": (Severity.ERROR, "submitted deployed runtime still contains a library placeholder"),
    "PLACEHOLDER_BAD_SCHEME": (Severity.ERROR, "library placeholder does not commit to the referenced FQN"),
    "LINK_REF_BAD": (Severity.ERROR, "declared linkReference does not align with the placeholder slot"),
    "ADDRESS_BAD_CHECKSUM": (Severity.ERROR, "library address fails EIP-55 checksum"),
    "ADDRESS_UNDECLARED": (Severity.ERROR, "linked address has no declared library mapping"),
    "RUNTIME_LEN_DIFF": (Severity.ERROR, "deployed runtime length differs"),
    "RUNTIME_ODD_HEX": (Severity.ERROR, "runtime code is not even-length hex"),
    "METADATA_MALFORMED": (Severity.ERROR, "embedded metadata JSON does not parse"),
    "CONTRACT_MISSING": (Severity.ERROR, "requested contract not present in compiler output"),
}


class Finding(BaseModel):
    code: str
    severity: Severity
    message: str
    detail: dict = Field(default_factory=dict)

    @classmethod
    def from_code(cls, code: str, message: str | None = None,
                  detail: dict | None = None,
                  severity_override: Severity | None = None) -> "Finding":
        sev, default = FINDING_CODES[code]
        return cls(code=code,
                   severity=severity_override or sev,
                   message=message or default,
                   detail=detail or {})


class ContractReport(BaseModel):
    fqn: str
    verdict: Verdict
    findings: list[Finding] = Field(default_factory=list)
    normalized_body_digest_expected: str | None = None
    normalized_body_digest_observed: str | None = None
    compared_library_slots: list[dict] = Field(default_factory=list)
    metadata_tail: dict = Field(default_factory=dict)
