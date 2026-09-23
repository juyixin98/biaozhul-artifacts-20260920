"""End-to-end pipeline: source -> AST -> CFG -> interval analysis."""

from __future__ import annotations
from dataclasses import dataclass

from .analyzer import AnalysisResult, Analyzer
from .errors import IvlError, Span
from .ir import build_cfg
from .parser import parse_source


@dataclass
class AnalyzeResult:
    result: AnalysisResult
    source: str

    # ---- serialization --------------------------------------------------

    def to_dict(self) -> dict:
        cfg = self.result.cfg
        invariants = []
        for b_id, st in enumerate(self.result.invariants):
            if st is None:
                invariants.append({"block": b_id, "reachable": False})
                continue
            invariants.append({
                "block": b_id,
                "reachable": True,
                "variables": {
                    name: iv.to_dict() for name, iv in sorted(st.items())
                },
            })
        alarms = [a.to_dict() for a in self.result.alarms]
        return {
            "status": "ok",
            "variables": list(cfg.vars),
            "arrays": dict(cfg.arrays),
            "num_blocks": len(cfg.blocks),
            "entry_block": cfg.entry,
            "invariants": invariants,
            "alarms": alarms,
            "alarm_count": len(alarms),
            "possible_alarm_count": sum(
                1 for a in self.result.alarms if a.certainty == "possible"),
            "certain_alarm_count": sum(
                1 for a in self.result.alarms if a.certainty == "certain"),
        }

    def alarm_spans(self) -> list[tuple[str, int]]:
        return [(a.kind, a.span.start_offset) for a in self.result.alarms]


def analyze_source(source: str) -> AnalyzeResult:
    program = parse_source(source)
    cfg = build_cfg(program, source)
    result = Analyzer(cfg).analyze()
    return AnalyzeResult(result, source)


def error_to_dict(e: IvlError) -> dict:
    d = {"status": "error", "error": type(e).__name__, "message": e.message}
    if e.span is not None:
        d["location"] = e.span.to_dict()
    return d
