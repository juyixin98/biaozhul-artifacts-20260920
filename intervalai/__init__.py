"""intervalai — interval abstract interpretation toolchain (backend only).

A small imperative language "Imp" implemented from scratch:

* hand-written lexer / recursive-descent parser (:mod:`intervalai.lexer`,
  :mod:`intervalai.parser`);
* semantic resolution (:mod:`intervalai.resolve`);
* integer-IR lowering and CFG construction (:mod:`intervalai.ir`);
* interval analysis over mathematical integers with widening & narrowing
  (:mod:`intervalai.intervals`, :mod:`intervalai.analyzer`);
* an independent concrete reference interpreter (:mod:`intervalai.concrete`);
* a JSON service (:mod:`intervalai.service`).

No part of parsing or analysis delegates to an external compiler framework.
"""

from .errors import IvalError, LexError, ParseError, SemanticError, ConcreteExecError
from .pipeline import (
    analyze_source,
    execute_source,
    parse_program,
    dump_cfg,
)

__all__ = [
    "IvalError", "LexError", "ParseError", "SemanticError", "ConcreteExecError",
    "analyze_source", "execute_source", "parse_program", "dump_cfg",
]
