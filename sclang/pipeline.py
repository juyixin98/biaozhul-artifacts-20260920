"""High-level toolchain entry points used by the CLI and JSON service."""

from dataclasses import dataclass

from . import ast_nodes as ast
from .closureconvert import convert_program
from .errors import CompileError, SclError
from .interpreter import Interpreter
from .ir import IRModule, disassemble
from .lexer import Lexer, Token
from .parser import Parser
from .resolver import Binding, ResolutionResult, resolve_program


@dataclass
class Frontend:
    program: ast.Program
    resolution: ResolutionResult
    tokens: list[Token]
    module: IRModule


def analyze(source: str) -> Frontend:
    """Run lex -> parse -> resolve -> closure-convert."""
    tokens = Lexer(source).tokenize()
    program = Parser(tokens, source).parse()
    resolution = resolve_program(program)
    module = convert_program(program, resolution)
    return Frontend(program=program, resolution=resolution,
                    tokens=tokens, module=module)


def run_frontend(fe: Frontend):
    """Execute converted IR on the independent VM; (result, output)."""
    from .vm import run_module
    return run_module(fe.module)


def run_reference(fe: Frontend):
    """Execute source AST on the tree-walking reference interpreter."""
    interp = Interpreter(fe.program, fe.resolution)
    result = interp.run()
    return result, interp.output


# -- JSON-friendly views ----------------------------------------------------

def error_payload(exc: SclError) -> dict:
    span = None
    if exc.span is not None:
        s = exc.span
        span = {"start": s.start, "end": s.end, "line": s.line, "col": s.col,
                "end_line": s.end_line, "end_col": s.end_col}
    stage = "compile" if isinstance(exc, CompileError) else "runtime"
    return {"ok": False, "stage": stage, "error": exc.message,
            "span": span,
            "snippet": exc.span.snippet() if exc.span is not None else None}


def tokens_json(tokens: list[Token]) -> list[dict]:
    out = []
    for t in tokens:
        s = t.span
        out.append({
            "kind": t.kind.name,
            "value": t.value,
            "span": [s.start, s.end, s.line, s.col, s.end_line, s.end_col],
        })
    return out


def _binding_json(b: Binding, depth_by_id: dict) -> dict:
    return {
        "name": b.name,
        "owner_func_id": b.owner,
        "owner_depth": depth_by_id.get(b.owner, -1),
        "kind": b.kind,
        "slot": b.slot, "captured": b.captured, "mutated": b.mutated,
        "boxed": b.boxed,
        "span": [b.span.start, b.span.end, b.span.line, b.span.col,
                 b.span.end_line, b.span.end_col],
    }


def analysis_json(fe: Frontend) -> dict:
    depth_by_id = {fi.func_id: fi.depth for fi in fe.resolution.functions}
    return {
        "functions": [
            {
                "func_id": fi.func_id,
                "ir_id": irf.id,
                "name": fi.name,
                "depth": fi.depth,
                "params": [_binding_json(b, depth_by_id) for b in fi.params],
                "free": [
                    {"name": b.name, "owner_func_id": b.owner,
                     "owner_depth": depth_by_id.get(b.owner, -1),
                     "boxed": b.boxed, "mutated": b.mutated}
                    for b in fi.free
                ],
                "slots": fi.slots,
            }
            for fi, irf in zip(fe.resolution.functions, fe.module.functions)
        ],
        "bindings": [_binding_json(b, depth_by_id)
                     for b in fe.resolution.bindings],
    }
