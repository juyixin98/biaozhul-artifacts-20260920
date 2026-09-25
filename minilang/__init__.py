"""Mini: a tiny language with hand-written parsing and incremental reparse.

Public API:

* :func:`lex` -- tokenise source text.
* :func:`full_parse` -- parse source text from scratch.
* :class:`Document` -- editable document with incremental reparsing.
* :class:`Node`, :func:`nodes_equal`, :func:`tree_signature`.
* :class:`Diagnostic`, :func:`result_to_dict`, :func:`node_to_dict`.
"""
from .document import Document, EditError, ParseResult, full_parse
from .lexer import (
    Diagnostic,
    Token,
    compute_line_starts,
    lex,
    offset_to_line_col,
)
from .nodes import Node, nodes_equal, shift_node, tree_signature
from .parser import Parser
from .serialize import diagnostic_to_dict, node_to_dict, result_to_dict

__all__ = [
    "Document",
    "EditError",
    "ParseResult",
    "full_parse",
    "lex",
    "Token",
    "Diagnostic",
    "compute_line_starts",
    "offset_to_line_col",
    "Node",
    "nodes_equal",
    "shift_node",
    "tree_signature",
    "Parser",
    "node_to_dict",
    "result_to_dict",
    "diagnostic_to_dict",
]
