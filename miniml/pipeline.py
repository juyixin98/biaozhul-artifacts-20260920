"""End-to-end pipeline helpers shared by the CLI and the HTTP service."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from .ast_nodes import Program
from .errors import render_compile_error
from .eval import EvalResult, Evaluator, TypePanic, MatchPanic
from .infer import InferError, InferenceResult, infer_program
from .lexer import LexError
from .parser import ParseError, parse
from .span import Span
from .types import scheme_str, type_str


@dataclass
class CompileFailure(Exception):
    phase: str  # 'lex' | 'parse' | 'infer' | 'eval'
    message: str
    span: Span
    rendered: str
    code: str


def compile_source(
    src: str, value_restriction: bool = True, trace: bool = True
) -> tuple[Program, InferenceResult]:
    """Parse and infer; raise :class:`CompileFailure` with a located message."""
    try:
        program = parse(src)
    except (LexError, ParseError) as err:
        rendered = render_compile_error(src, err)
        code = "E001" if isinstance(err, LexError) else "E002"
        raise CompileFailure(
            "parse" if isinstance(err, ParseError) else "lex",
            err.message,
            err.span,
            rendered,
            code,
        ) from err

    try:
        result = infer_program(program, value_restriction=value_restriction, trace=trace)
    except InferError as err:
        rendered = render_compile_error(src, err)
        code = "E004" if err.cycle else "E003"
        raise CompileFailure(
            "infer", err.message, err.span, rendered, code
        ) from err
    return program, result


def evaluate(program: Program) -> EvalResult:
    try:
        return Evaluator().eval_program(program)
    except (TypePanic, MatchPanic) as err:
        # Evaluation runs only on programs that type-checked; a panic here in
        # the value_restriction=False path is the evidence of unsoundness.
        raise CompileFailure(
            "eval",
            err.message,
            err.span,
            f"runtime panic: {err.message} at line {err.span.start.line}",
            "E005",
        ) from err


def serialize_result(result: InferenceResult, evaluate_output: Optional[str] = None) -> dict:
    payload: dict = {
        "bindings": [
            {
                "name": b.name,
                "type": scheme_str(b.scheme),
                "monomorphic": b.monomorphic,
                "quantified": [f"t{q.id}" for q in b.scheme.qvars],
            }
            for b in result.bindings
        ],
    }
    if result.expr_type is not None:
        payload["final_type"] = type_str(result.expr_type)
    if result.trace:
        payload["trace"] = [
            {
                "step": ev.step,
                "detail": ev.detail,
                "location": f"{ev.span.start.line}:{ev.span.start.col}"
                if ev.span is not None
                else None,
            }
            for ev in result.trace
        ]
    if evaluate_output is not None:
        payload["eval_output"] = evaluate_output
    return payload
