"""MiniML: a tiny polymorphic functional language toolchain.

Modules
-------
- span      : source positions and spans
- lexer     : hand-written lexer
- parser    : hand-written recursive-descent / Pratt parser
- ast_nodes : AST definition
- types     : type representation (union-find), unification, schemes
- infer     : Algorithm W (let-polymorphism, occurs check, value restriction)
- eval      : small CBV evaluator used to demonstrate the reference unsoundness
- errors    : rendering of compiler-style error messages with source locations
- service   : JSON over HTTP service (stdlib only)
"""

from .span import Pos, Span
from .lexer import lex, Token
from .parser import parse
from .ast_nodes import Program
from .infer import infer_program, InferenceResult, Inferrer
from .types import Type, TVar, TCon, TApp, scheme_str, type_str
from .eval import eval_program, EvalResult
from .errors import render_compile_error

__all__ = [
    "Pos",
    "Span",
    "lex",
    "Token",
    "parse",
    "Program",
    "infer_program",
    "InferenceResult",
    "Inferrer",
    "Type",
    "TVar",
    "TCon",
    "TApp",
    "scheme_str",
    "type_str",
    "eval_program",
    "EvalResult",
    "render_compile_error",
]
