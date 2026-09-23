"""差分语法解析工具链（dlang）。

仅依赖 Python 标准库，包含词法分析、全量语法分析、增量语法分析
和一个基于 http.server 的 JSON 服务。
"""

from .nodes import Node, ParseError
from .lexer import LexError, Token, tokenize
from .parser import parse
from .incremental import Edit, Document

__all__ = [
    "Node",
    "ParseError",
    "LexError",
    "Token",
    "tokenize",
    "parse",
    "Edit",
    "Document",
]
