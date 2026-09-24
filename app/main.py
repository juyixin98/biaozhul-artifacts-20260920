"""FastAPI application exposing the taint analysis service."""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, field_validator

from .analysis import AnalysisRequest, run_analysis
from .language import LexError, ParseError
from .signing import KeyPair, load_or_create_keypair, sign_report, verify_report

app = FastAPI(
    title="Expression Taint Analysis Service",
    version="1.0.0",
    description=(
        "Static taint analysis for a small scripting language. "
        "Every report is signed with Ed25519; fetch the public key at "
        "GET /signing-key and verify with POST /verify or offline."
    ),
)

_keypair: KeyPair | None = None


def _keys() -> KeyPair:
    global _keypair
    if _keypair is None:
        _keypair = load_or_create_keypair()
    return _keypair


class AnalyzeIn(BaseModel):
    code: str = Field(..., min_length=1, description="program source code")
    entry: str = Field("main", description="entry function name")
    sources: list[str] = Field(default_factory=list)
    sanitizers: list[str] = Field(default_factory=list)
    validators: list[str] = Field(default_factory=list)
    sinks: list[str] = Field(default_factory=list)
    propagators: dict[str, list[int]] | None = Field(
        default=None,
        description="extra propagators: name -> propagating arg indices "
                    "(empty list means all args)",
    )
    call_string_k: int = Field(3, ge=0, le=8)

    @field_validator("code")
    @classmethod
    def _non_empty(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("code must not be empty")
        return v


@app.get("/health")
def health() -> dict:
    return {"status": "ok", "service": "taint-analysis", "version": "1.0.0"}


@app.get("/signing-key")
def signing_key() -> dict:
    from cryptography.hazmat.primitives import serialization
    pub = _keys().public.public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    ).decode("ascii")
    return {"algorithm": "Ed25519", "public_key_pem": pub}


@app.post("/analyze")
def analyze(body: AnalyzeIn) -> JSONResponse:
    propagators = None
    if body.propagators is not None:
        propagators = {k: tuple(v) for k, v in body.propagators.items()}
    req = AnalysisRequest(
        code=body.code,
        entry=body.entry,
        sources=tuple(body.sources),
        sanitizers=tuple(body.sanitizers),
        validators=tuple(body.validators),
        sinks=tuple(body.sinks),
        propagators=propagators,
        call_string_k=body.call_string_k,
    )
    try:
        report = run_analysis(req)
    except (ParseError, LexError) as exc:
        return JSONResponse(
            status_code=400,
            content={"verdict": "parse_error", "error": str(exc)},
        )
    signed = sign_report(report, _keys())
    return JSONResponse(status_code=200, content=signed)


class VerifyIn(BaseModel):
    report: dict[str, Any]


@app.post("/verify")
def verify(body: VerifyIn) -> dict:
    ok = verify_report(body.report)
    verdict = body.report.get("verdict", "unknown")
    return {"valid_signature": ok, "verdict": verdict if ok else "unverified"}
