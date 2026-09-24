"""FastAPI application exposing the taint analysis service."""
from __future__ import annotations

from pydantic import BaseModel, Field

from fastapi import FastAPI
from fastapi.responses import PlainTextResponse

from .analyzer import AnalysisError, Analyzer
from .crypto_sign import Signer, verify
from .lexer import LexError
from .parser import ParseError, parse

signer = Signer()
app = FastAPI(
    title="TaintLang Static Taint Analysis Service",
    version="1.0.0",
    description=(
        "Static may-taint analysis for a small scripting language with "
        "assignment, branches, loops, functions and preset "
        "source()/sink()/sanitize() builtins. Reports evidence paths from "
        "sources to sinks and signs each report with an Ed25519 key."),
)


class AnalyzeRequest(BaseModel):
    code: str = Field(..., description="TaintLang source code to analyze",
                      examples=["x = source();\nsink(x);\n"])
    entry: str | None = Field(
        default=None,
        description="Reserved: the language has a single top-level entry point.",
        deprecated=True)


@app.get("/health")
def health() -> dict:
    return {"status": "ok", "service": "taint-analysis", "version": app.version}


@app.get("/public-key", response_class=PlainTextResponse)
def public_key() -> str:
    return signer.public_key_pem().decode("ascii")


@app.post("/analyze")
def analyze(req: AnalyzeRequest) -> dict:
    try:
        program = parse(req.code)
        result = Analyzer(program).analyze()
    except (LexError, ParseError, AnalysisError) as exc:
        return {
            "ok": False,
            "error": {"type": type(exc).__name__, "message": str(exc)},
        }
    report = {
        "ok": True,
        "language": "TaintLang-1",
        "vulnerable": result["vulnerable"],
        "finding_count": len(result["findings"]),
        "findings": result["findings"],
        "stats": result["stats"],
        "analysis": {
            "kind": "static may-taint dataflow",
            "context_sensitivity": (
                "0-CFA context-insensitive interprocedural summaries with "
                "symbolic parameter-origin substitution; flow-sensitive and "
                "field-insensitive; path-insensitive; implicit flows ignored"),
            "fixpoint": "monotone union fixpoint (chaotic worklist per "
                        "function; Tarjan SCC iteration for recursion)",
            "false_positive_boundary": (
                "Both arms of every if/while merge at joins: sanitizing on "
                "only one branch arm is still reported as vulnerable. "
                "No false negatives for explicit data flow through "
                "assignment, operators, function args/returns and "
                "recursion under this language's semantics."),
        },
    }
    return {"report": report, "signature": signer.sign(report)}


@app.post("/verify-signature")
def verify_signature(payload: dict) -> dict:
    report = payload.get("report")
    signature = payload.get("signature")
    if not isinstance(report, dict) or not isinstance(signature, str):
        return {"valid": False, "error": "expected {'report': {...}, 'signature': '...'}"}
    valid = verify(signer.public_key_pem(), signature, report)
    return {"valid": valid}
