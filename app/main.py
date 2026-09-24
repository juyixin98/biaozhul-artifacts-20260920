"""FastAPI application: offline X.509 chain verification HTTP API."""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from cryptography.hazmat.primitives.serialization import Encoding

from .pki import CertLoadError, load_certificate, load_certificates
from .verifier import (
    PURPOSE_CLIENT,
    PURPOSE_SERVER,
    VALID_PURPOSES,
    VerificationInputError,
    verify_chain,
)

app = FastAPI(
    title="Offline X.509 Chain Verification API",
    version="1.0.0",
    description=(
        "Verify an X.509 leaf certificate against an explicit set of trust "
        "roots and candidate intermediates. Fully offline: no OCSP, CRL or "
        "any other network access; revocation is not checked."
    ),
)


class VerifyRequest(BaseModel):
    leaf_certificate: str = Field(
        ...,
        description="Leaf certificate in PEM format (DER/base64 also accepted).",
    )
    intermediate_certificates: list[str] = Field(
        default_factory=list,
        description=(
            "Candidate intermediate CA certificates, each PEM/DER/base64; a PEM "
            "bundle with multiple blocks is also accepted per item. Untrusted: "
            "only used for path building; a self-signed certificate here is "
            "NEVER accepted as a trust anchor."
        ),
    )
    trust_roots: list[str] = Field(
        ...,
        min_length=1,
        description=(
            "Explicit trust anchors (self-signed root CAs). At least one is "
            "required. Only these certificates can terminate the chain."
        ),
    )
    purpose: str = Field(
        default=PURPOSE_SERVER,
        description=f"Verification purpose: {PURPOSE_SERVER} or {PURPOSE_CLIENT}.",
    )
    subject: str | None = Field(
        default=None,
        description=(
            "Expected leaf identity for server_auth: a DNS name "
            "(wildcards in cert SAN supported, RFC 6125) or an IP literal."
        ),
        examples=["example.com", "10.0.0.5"],
    )
    verification_time: str | None = Field(
        default=None,
        description=(
            "ISO 8601 timezone-aware timestamp to validate at. "
            "Defaults to the current UTC time."
        ),
        examples=["2026-09-24T12:00:00Z"],
    )
    max_chain_depth: int | None = Field(
        default=None,
        ge=0,
        description=(
            "Optional hard limit on chain depth (number of intermediate CAs). "
            "Unset = engine default."
        ),
    )


class _Error(BaseModel):
    error: str
    detail: str


@app.exception_handler(VerificationInputError)
async def input_error_handler(_: Request, exc: VerificationInputError) -> JSONResponse:
    return JSONResponse(
        status_code=400,
        content={"error": "invalid_request", "detail": str(exc)},
    )


@app.exception_handler(CertLoadError)
async def cert_load_error_handler(_: Request, exc: CertLoadError) -> JSONResponse:
    return JSONResponse(
        status_code=422,
        content={"error": "certificate_parse_error", "detail": str(exc)},
    )


def _collect(items: list[str]) -> list:
    """Parse request cert items, de-duplicating by DER encoding."""
    out: list = []
    seen: set[bytes] = set()
    for item in items:
        for cert in load_certificates(item):
            key = cert.public_bytes(Encoding.DER)
            if key not in seen:
                seen.add(key)
                out.append(cert)
    return out


@app.get("/health", tags=["meta"])
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post(
    "/v1/verify",
    tags=["verify"],
    summary="Verify an X.509 certificate chain offline",
    responses={
        200: {"description": "Verification performed (see body.valid for verdict)"},
        400: {"model": _Error, "description": "Invalid request parameters"},
        422: {"model": _Error, "description": "A certificate could not be parsed"},
    },
)
async def verify(req: VerifyRequest) -> dict[str, Any]:
    if req.purpose not in VALID_PURPOSES:
        raise VerificationInputError(
            f"purpose must be one of {list(VALID_PURPOSES)}, got {req.purpose!r}"
        )

    leaf = load_certificate(req.leaf_certificate)
    intermediates = _collect(req.intermediate_certificates)
    trust_roots = _collect(req.trust_roots)

    result = verify_chain(
        leaf,
        intermediates,
        trust_roots,
        purpose=req.purpose,
        subject=req.subject,
        verification_time=req.verification_time,
        max_chain_depth=req.max_chain_depth,
    )
    return result.to_dict()
