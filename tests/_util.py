"""Shared helpers for the test suite."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from lattlang.interp_ast import run_ast
from lattlang.interp_ir import run_ir
from lattlang.irbuild import build_ir
from lattlang.optimize import optimize
from lattlang.parser import parse
from lattlang import ssa as ssa_mod


EXAMPLES = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "examples")


def compile3(source: str):
    """Return (ast_result, ssa_ir_result, optimized_result)."""
    program = parse(source)
    cfg = build_ir(program)
    ssa_prog = ssa_mod.build_ssa(cfg)
    opt_prog = optimize(ssa_prog).program
    return run_ast(program), run_ir(ssa_prog), run_ir(opt_prog)


def signature(r):
    return (tuple(r.output),
            None if r.ok else (r.error.stage, r.error.message))


def equivalent(source: str) -> bool:
    a, i, o = compile3(source)
    return signature(a) == signature(i) == signature(o)


def example_path(name: str) -> str:
    return os.path.join(EXAMPLES, name)


def read_example(name: str) -> str:
    with open(example_path(name), "r", encoding="utf-8") as f:
        return f.read()
