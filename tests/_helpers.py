"""Shared helpers for the ScL test-suite."""
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from sclang.parser import parse_source
from sclang.resolver import resolve_program
from sclang.interpreter import Interpreter
from sclang.closureconvert import convert_program
from sclang.vm import run_module as vm_run
from sclang.pipeline import analyze


def compile_only(source):
    return analyze(source)


def reference_output(source):
    prog = parse_source(source)
    res = resolve_program(prog)
    it = Interpreter(prog, res)
    it.run()
    return it.output


def vm_output(source):
    prog = parse_source(source)
    res = resolve_program(prog)
    mod = convert_program(prog, res)
    _, out = vm_run(mod)
    return out


def both_outputs(source):
    prog = parse_source(source)
    res = resolve_program(prog)
    it = Interpreter(prog, res)
    it.run()
    mod = convert_program(prog, res)
    _, out = vm_run(mod)
    return it.output, out
