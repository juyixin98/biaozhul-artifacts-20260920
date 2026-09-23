"""End-to-end driver: source -> raw IR -> SSA IR -> executable IR.

Also captures structured details (dominance, phi placement, pruned blocks,
execution comparison) for the JSON service and tests.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from . import ir as ir_mod
from .analysis.dominance import compute_dominance
from .analysis.ssa_construct import construct_ssa
from .analysis.ssa_destroy import eliminate_phis
from .analysis.verify import verify_ssa
from .errors import MiniError
from .frontend.builder import build_module
from .interp import Interpreter, InterpResult
from .parser import parse


@dataclass
class FuncPipeline:
    name: str
    param_names: list[str]
    raw: ir_mod.Function
    ssa: ir_mod.Function
    flat: ir_mod.Function
    removed: list[str]
    variables: list[str]
    phi_blocks: dict[str, list[str]]
    split_edges: list[str]
    swap_temps: int
    verify_notes: list[str]


@dataclass
class PipelineResult:
    funcs: dict[str, FuncPipeline] = field(default_factory=dict)
    raw_module: object = None
    ssa_module: object = None
    flat_module: object = None
    executions: dict[str, dict] = field(default_factory=dict)


def _param_source_names(program) -> dict[str, list[str]]:
    return {f.name: list(f.params) for f in program.funcs}


def run_pipeline(source: str, file: str = "<input>",
                 entry: str = "main",
                 inputs: Optional[list[int]] = None,
                 execute: bool = True) -> PipelineResult:
    program = parse(source, file)
    names_by_func = _param_source_names(program)
    raw_module, lower_info = build_module(program)

    ssa_mod = ir_mod.Module()
    flat_mod = ir_mod.Module()

    result = PipelineResult(raw_module=raw_module)

    for name, raw_fn in raw_module.funcs.items():
        param_names = names_by_func.get(name, [])
        ssa_res = construct_ssa(raw_fn, param_names=param_names)
        notes = verify_ssa(ssa_res.func)
        dest = eliminate_phis(ssa_res.func)

        ssa_mod.add_function(ssa_res.func.copy())
        flat_mod.add_function(dest.func)

        result.funcs[name] = FuncPipeline(
            name=name,
            param_names=param_names,
            raw=raw_fn,
            ssa=ssa_res.func,
            flat=dest.func,
            removed=ssa_res.removed,
            variables=ssa_res.variables,
            phi_blocks=ssa_res.phi_blocks,
            split_edges=dest.split_edges,
            swap_temps=dest.swap_temps,
            verify_notes=notes,
        )

    result.ssa_module = ssa_mod
    result.flat_module = flat_mod

    if execute and entry in raw_module.funcs and inputs is not None:
        r1 = Interpreter(raw_module, "memory",
                         param_names=names_by_func).run(entry, list(inputs))
        r2 = Interpreter(ssa_mod, "ssa").run(entry, list(inputs))
        r3 = Interpreter(flat_mod, "flat").run(entry, list(inputs))
        match = (r1.return_value == r2.return_value == r3.return_value
                 and r1.output == r2.output == r3.output)
        result.executions[entry] = {
            "inputs": list(inputs),
            "memory": _exec_json(r1),
            "ssa": _exec_json(r2),
            "flat": _exec_json(r3),
            "agree": match,
        }

    return result


def _exec_json(r: InterpResult) -> dict:
    return {"return": r.return_value, "printed": r.output, "steps": r.steps}


# ----------------------------------------------------------------- JSON view

def _loc_json(loc):
    return loc.to_json() if loc is not None else None


def dom_summary(fn) -> dict:
    dom = compute_dominance(fn, prune_unreachable=False)
    return {
        "reachable": dom.labels,
        "unreachable": dom.removed,
        "immediate_dominators": {b: d for b, d in dom.idom.items() if d is not None},
        "dominance_frontiers": {b: sorted(s) for b, s in dom.df.items() if s},
        "dominator_tree": dom.children,
    }


def pipeline_to_json(res: PipelineResult, include_ir: bool = True) -> dict:
    out: dict = {"functions": {}}
    for name, p in res.funcs.items():
        entry = {
            "params": p.param_names,
            "variables_promoted": p.variables,
            "unreachable_blocks_pruned": p.removed,
            "phi_insertions": {v: bs for v, bs in sorted(p.phi_blocks.items())},
            "critical_edges_split": p.split_edges,
            "swap_temporaries_introduced": p.swap_temps,
            "verify": p.verify_notes,
            "dominance_raw": dom_summary(p.raw),
        }
        if include_ir:
            entry["ir_raw"] = ir_mod.dump_function(p.raw)
            entry["ir_ssa"] = ir_mod.dump_function(p.ssa)
            entry["ir_flat"] = ir_mod.dump_function(p.flat)
        out["functions"][name] = entry
    if res.executions:
        out["executions"] = res.executions
    return out


def error_to_json(err: MiniError, source: str | None = None) -> dict:
    return {
        "ok": False,
        "error": {
            "kind": type(err).__name__,
            "message": err.message,
            "loc": _loc_json(err.loc),
            "rendered": err.render(source),
        },
    }
