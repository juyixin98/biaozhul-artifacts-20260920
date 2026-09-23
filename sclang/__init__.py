"""ScL (Scope/closure Language) — a small lexically-scoped language toolchain.

Pipeline:
    source text
      -> lexer.Lexer        (tokens, each with a source Span)
      -> parser.Parser      (AST annotated with spans)
      -> resolver.Resolver  (lexical binding analysis + closure capture sets)
      -> closureconvert     (explicit environment/cell IR)
      -> vm.VM              (independent stack interpreter for the IR)

``interpreter`` is a separate tree-walking reference interpreter over the
AST; it shares nothing with the VM except the AST definition, and is the
oracle the differential tests compare the transformed program against.
"""

from .errors import CompileError, RuntimeError_, SclError, Span

__all__ = [
    "Span",
    "SclError",
    "CompileError",
    "RuntimeError_",
]
