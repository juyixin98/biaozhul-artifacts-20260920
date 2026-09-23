"""High-level pipeline helpers tying the toolchain phases together."""

from __future__ import annotations

from .analyzer import analyze
from .errors import LangError
from .ir_interp import IRInterpreter
from .lower import lower_module
from .parser import parse
from .source_interp import SourceInterpreter


def compile_source(source: str, filename: str = "<input>"):
    """Run lex -> parse -> analyze -> closure convert.  Returns (ast, analysis, module)."""
    tree = parse(source, filename)
    analysis = analyze(tree)
    module = lower_module(tree, analysis)
    return tree, analysis, module


def run_ir(source: str, filename: str = "<input>", trace: list | None = None):
    _, _, module = compile_source(source, filename)
    return IRInterpreter(module, trace=trace).run()


def run_source(source: str, filename: str = "<input>", trace: list | None = None):
    tree = parse(source, filename)
    return SourceInterpreter(tree, trace=trace).run()
