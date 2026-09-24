"""High-level analysis facade: source text -> structured report."""

from __future__ import annotations

from dataclasses import dataclass

from ..language import Program, parse
from .engine import AnalysisResult, TaintEngine
from .evidence import Chain
from .policy import PolicyExtension, build_policy
from .taint import Taint


@dataclass(frozen=True)
class AnalysisRequest:
    code: str
    entry: str = "main"
    sources: tuple[str, ...] = ()
    sanitizers: tuple[str, ...] = ()
    validators: tuple[str, ...] = ()
    sinks: tuple[str, ...] = ()
    propagators: dict[str, tuple[int, ...]] | None = None
    call_string_k: int = 3


def run_analysis(req: AnalysisRequest) -> dict:
    program: Program = parse(req.code, entry=req.entry)
    extension = PolicyExtension(
        sources=req.sources,
        sanitizers=req.sanitizers,
        validators=req.validators,
        sinks=req.sinks,
        propagators=req.propagators,
    )
    policy = build_policy(extension)
    engine = TaintEngine(program, policy, call_string_k=req.call_string_k)
    result = engine.analyze()
    return _serialize(program, result, req)


def _serialize(program: Program, result: AnalysisResult,
               req: AnalysisRequest) -> dict:
    findings = []
    for rec in result.findings:
        chains = sorted(
            rec.value.chains,
            key=lambda c: (c.source_name or "", len(c.hops)),
        )
        evidence_paths = [
            {
                "source": chain.source_name,
                "truncated": chain.truncated,
                "hops": chain.to_dicts(),
                "rendered": chain.render(),
            }
            for chain in chains
        ]
        sink_hop = {
            "kind": "sink",
            "detail": f"{rec.sink}(arg {rec.arg_index})",
            "function": rec.function,
            "line": rec.line,
            "col": rec.col,
        }
        for ep in evidence_paths:
            ep["hops"].append(sink_hop)
            ep["rendered"] = ep["rendered"] + (
                f" -> {rec.function}@{rec.line}: sink({rec.sink})"
            )
        findings.append({
            "sink": rec.sink,
            "function": rec.function,
            "context": rec.context,
            "line": rec.line,
            "col": rec.col,
            "argument": rec.arg_index,
            "taint_level": rec.value.level.value,
            "certainty": (
                "definite" if rec.value.level is Taint.TAINT else "conditional"
            ),
            "evidence_paths": evidence_paths,
        })

    return {
        "entry": req.entry,
        "verdict": "vulnerable" if findings else "safe",
        "finding_count": len(findings),
        "findings": findings,
        "context_sensitivity": {
            "kind": "call-string (context-sensitive, flow-sensitive)",
            "call_string_k": req.call_string_k,
            "contexts": result.contexts,
        },
        "reachable_functions": result.reachable_functions,
        "fixpoint_iterations": result.iterations,
        "warnings": result.warnings,
    }
