"""High-level pipeline facade: source text -> lex -> parse -> IR -> analysis."""

from __future__ import annotations

from typing import Optional

from .analyzer import Analyzer
from .builder import build
from .config import Config
from .errors import TaintLangError
from .parser import parse


def analyze_source(source: str, config: Optional[Config] = None,
                   tainted_entry_params: bool = True) -> dict:
    """Run the complete toolchain on ``source`` and return the result dict.

    Raises a :class:`~taintlang.errors.TaintLangError` subclass on
    lexical, syntactic, build-time or configuration errors.
    """
    config = config or Config()
    program_ast = parse(source)
    program_ir = build(program_ast, config, source)
    analyzer = Analyzer(program_ir, config,
                        tainted_entry_params=tainted_entry_params)
    result = analyzer.analyze()
    result["ir"] = dump_ir(program_ir)
    return result


def dump_ir(program_ir) -> dict:
    """Serialise the IR (useful for tests and debugging)."""
    funcs = []
    for func in program_ir.functions:
        blocks = []
        for block in func.blocks:
            instrs = []
            for i in block.instructions:
                entry = {"uid": i.uid, "op": type(i).__name__,
                         "span": i.span.to_dict()}
                # explicit field dump (dataclasses vary per instr type)
                for field_name in ("dst", "src", "arg", "value", "kind",
                                   "name", "op", "operand", "left", "right",
                                   "args", "return_reg"):
                    if hasattr(i, field_name):
                        v = getattr(i, field_name)
                        entry[field_name] = list(v) if isinstance(v, tuple) else v
                instrs.append(entry)
            t = block.terminator
            term = {"op": type(t).__name__, "span": t.span.to_dict()}
            for field_name in ("target", "cond", "then_target", "else_target",
                               "value", "callee", "args", "return_reg",
                               "cont", "site_uid"):
                if hasattr(t, field_name):
                    v = getattr(t, field_name)
                    term[field_name] = list(v) if isinstance(v, tuple) else v
            blocks.append({
                "label": block.label,
                "instructions": instrs,
                "terminator": term,
            })
        funcs.append({
            "name": func.name,
            "params": list(func.params),
            "entry": func.entry,
            "blocks": blocks,
        })
    return {
        "functions": funcs,
        "call_sites": [
            {"site_uid": s, "caller": c, "callee": e, "span": sp.to_dict()}
            for s, c, e, sp in program_ir.call_sites
        ],
    }
