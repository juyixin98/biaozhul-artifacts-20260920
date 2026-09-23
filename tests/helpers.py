"""Shared helpers for the test suite."""
from __future__ import annotations

import os

from ssa_toolchain.interp import Interpreter
from ssa_toolchain.pipeline import run_pipeline

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EXAMPLES = os.path.join(ROOT, "examples")


def compile_source(src: str, inputs=None, entry="main"):
    return run_pipeline(src, file="<test>", entry=entry, inputs=inputs,
                        execute=inputs is not None)


def example_path(name: str) -> str:
    return os.path.join(EXAMPLES, name)


def load_example(name: str):
    with open(example_path(name), encoding="utf-8") as fh:
        return fh.read()


def exec_raw_and_flat(raw_mod, flat_mod, inputs, ssa_mod=None,
                      param_names=None):
    r1 = Interpreter(raw_mod, "memory", param_names=param_names or {}) \
        .run("main", list(inputs))
    r3 = Interpreter(flat_mod, "flat").run("main", list(inputs))
    r2 = None
    if ssa_mod is not None:
        r2 = Interpreter(ssa_mod, "ssa").run("main", list(inputs))
    return r1, r2, r3
