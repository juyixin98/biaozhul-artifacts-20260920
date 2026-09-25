"""JSON service for LattLang.

Reads one request object from stdin (or a file argument) and writes one
response object to stdout.  No HTTP, no third-party dependencies.

Request:  {"action": ..., "source": "...", ...}
Response: {"ok": true, ...} on success; {"ok": false, "error": {...}} on a
structured compiler/runtime error.

Actions
-------
analyze
    Runs SCCP and returns reachable blocks, executable edges and the
    lattice cell of every SSA value / phi / terminator condition.
optimize
    Returns optimized IR text plus the list of changes applied.
run
    Runs one of ``ast`` / ``ir`` / ``optimized`` and returns output lines,
    step count and (if any) the runtime error with its source span.
check
    Runs the source three ways (AST, SSA IR, optimized IR) and reports
    whether observable output and error kind/location agree.
"""

from __future__ import annotations

import json
import sys
from typing import Any

from .errors import LangError
from .interp_ast import run_ast
from .interp_ir import run_ir
from .ir import print_ir
from .irbuild import build_ir
from .optimize import optimize
from .parser import parse
from .analysis import run_sccp
from . import ssa as ssa_mod


# --------------------------------------------------------------------------
# Serialization helpers
# --------------------------------------------------------------------------

def _run_payload(result) -> dict[str, Any]:
    return {
        "output": result.output,
        "stdout": result.stdout(),
        "steps": result.steps,
        "error": None if result.ok else result.error.to_json(),
    }


def _value_cell(res, op) -> dict[str, Any]:
    from .ir import Imm
    if isinstance(op, Imm):
        return {"kind": "const", "value": op.value}
    return res.value_of(op).to_json()


# --------------------------------------------------------------------------
# Actions
# --------------------------------------------------------------------------

def action_analyze(source: str) -> dict[str, Any]:
    program = parse(source)
    cfg = build_ir(program)
    ssa_prog = ssa_mod.build_ssa(cfg)
    res = run_sccp(ssa_prog)

    blocks: list[dict[str, Any]] = []
    for b in ssa_prog.blocks:
        entry: dict[str, Any] = {
            "label": b.label,
            "reachable": b.label in res.reachable,
            "preds": b.preds,
            "succs": b.succs,
            "phis": [],
            "instructions": [],
            "terminator": {"kind": b.term.kind, "span": b.term.span.to_json()},
        }
        for dest, args in b.phis.items():
            entry["phis"].append({
                "dest": dest,
                "value": res.value_of(dest).to_json(),
                "args": [
                    {"from": b.preds[i] if i < len(b.preds) else None,
                     "operand": str(a.value) if hasattr(a, "value") else a,
                     "value": res.value_of(a).to_json()}
                    for i, a in enumerate(args)
                ],
            })
        for ins in b.instrs:
            row: dict[str, Any] = {
                "kind": ins.kind,
                "op": ins.op,
                "dest": ins.dest,
                "span": ins.span.to_json(),
            }
            if ins.dest is not None:
                row["value"] = res.value_of(ins.dest).to_json()
            entry["instructions"].append(row)
        if b.term.kind == "br":
            entry["terminator"]["condition"] = _value_cell(res, b.term.cond)
            entry["terminator"]["edges"] = [
                {"to": b.term.targets[0],
                 "executable": res.is_edge_executable(b.label, b.term.targets[0])},
                {"to": b.term.targets[1],
                 "executable": res.is_edge_executable(b.label, b.term.targets[1])},
            ]
        blocks.append(entry)

    return {
        "reachable_blocks": sorted(res.reachable),
        "executable_edges": [[a, c] for a, c in sorted(res.executable_edges)],
        "blocks": blocks,
        "lattice_summary": _lattice_summary(res),
    }


def _lattice_summary(res) -> dict[str, int]:
    summary = {"top": 0, "const": 0, "bottom": 0}
    for v in res.lat.values():
        summary[v.kind] += 1
    return summary


def action_optimize(source: str, include_ir: bool = True) -> dict[str, Any]:
    program = parse(source)
    cfg = build_ir(program)
    ssa_prog = ssa_mod.build_ssa(cfg)
    res = run_sccp(ssa_prog)
    report = optimize(ssa_prog, res)
    out: dict[str, Any] = {
        "changes": report.changes,
        "change_count": report.change_count(),
        "reachable_before": sorted(res.reachable),
    }
    if include_ir:
        out["ssa_ir"] = print_ir(ssa_prog)
        out["optimized_ir"] = print_ir(report.program)
    return out


def action_run(source: str, mode: str = "optimized",
               max_steps: int | None = None) -> dict[str, Any]:
    program = parse(source)
    if mode == "ast":
        result = run_ast(program) if max_steps is None else run_ast(program, max_steps)
        return {"mode": "ast", **_run_payload(result)}
    cfg = build_ir(program)
    ssa_prog = ssa_mod.build_ssa(cfg)
    if mode == "ir":
        target = ssa_prog
    elif mode == "optimized":
        target = optimize(ssa_prog).program
    else:
        raise LangError(f"unknown run mode {mode!r}", "service")
    result = run_ir(target) if max_steps is None else run_ir(target, max_steps)
    return {"mode": mode, **_run_payload(result)}


def action_check(source: str, max_steps: int | None = None) -> dict[str, Any]:
    program = parse(source)
    cfg = build_ir(program)
    ssa_prog = ssa_mod.build_ssa(cfg)
    opt_prog = optimize(ssa_prog).program

    ast_r = run_ast(program) if max_steps is None else run_ast(program, max_steps)
    ir_r = run_ir(ssa_prog) if max_steps is None else run_ir(ssa_prog, max_steps)
    opt_r = run_ir(opt_prog) if max_steps is None else run_ir(opt_prog, max_steps)

    def sig(r):
        return {
            "output": r.output,
            "error_stage": None if r.ok else r.error.stage,
            "error_message": None if r.ok else r.error.message,
            "error_span": None if (r.ok or r.error.span is None)
                           else r.error.span.to_json(),
        }

    signatures = {
        "ast": sig(ast_r),
        "ir": sig(ir_r),
        "optimized": sig(opt_r),
    }
    equivalent = sig(ast_r) == sig(ir_r) == sig(opt_r)
    return {
        "equivalent": equivalent,
        "signatures": signatures,
        "steps": {"ast": ast_r.steps, "ir": ir_r.steps,
                  "optimized": opt_r.steps},
    }


# --------------------------------------------------------------------------
# Dispatch
# --------------------------------------------------------------------------

def handle(request: dict[str, Any]) -> dict[str, Any]:
    action = request.get("action")
    source = request.get("source", "")
    if not isinstance(source, str):
        raise LangError("'source' must be a string", "service")

    if action == "analyze":
        return action_analyze(source)
    if action == "optimize":
        return action_optimize(source, request.get("include_ir", True))
    if action == "run":
        return action_run(source, request.get("mode", "optimized"),
                          request.get("max_steps"))
    if action == "check":
        return action_check(source, request.get("max_steps"))
    raise LangError(
        f"unknown action {action!r}; expected analyze|optimize|run|check",
        "service")


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    try:
        if argv:
            with open(argv[0], "r", encoding="utf-8") as f:
                request = json.load(f)
        else:
            request = json.load(sys.stdin)
        response = handle(request)
        response = {"ok": True, "action": request.get("action"), **response}
    except LangError as e:
        response = {"ok": False, "error": e.to_json()}
    except json.JSONDecodeError as e:
        response = {"ok": False,
                    "error": {"stage": "service",
                              "message": f"invalid JSON request: {e}"}}
    except OSError as e:
        response = {"ok": False,
                    "error": {"stage": "service", "message": str(e)}}
    json.dump(response, sys.stdout, ensure_ascii=False, indent=2)
    sys.stdout.write("\n")
    return 0 if response.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
