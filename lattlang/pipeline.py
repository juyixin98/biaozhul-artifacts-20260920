"""End-to-end pipeline helpers shared by the CLI, service and tests."""

from __future__ import annotations

from dataclasses import dataclass

from . import ssa as ssa_mod
from .analysis import SCCPResult, run_sccp
from .irbuild import build_ir
from .optimize import OptimizationReport, optimize
from .parser import parse


@dataclass
class Compiled:
    ast: object
    ir: object              # non-SSA CFG IR
    ssa: object             # SSA IR
    analysis: SCCPResult
    optimized: OptimizationReport


def compile_source(source: str) -> Compiled:
    program = parse(source)
    cfg = build_ir(program)
    ssa_prog = ssa_mod.build_ssa(cfg)
    result = run_sccp(ssa_prog)
    report = optimize(ssa_prog, result)
    return Compiled(program, cfg, ssa_prog, result, report)
