"""tinyinfer：带 let 多态的函数式小语言工具链。

纯手写词法/语法分析与基于合一的类型推导（算法 W），
不依赖任何现成编译器或解析框架。
"""
from .errors import LexError, ParseError, TinyError, TypeError_
from .locations import Position, Span

__all__ = [
    "LexError",
    "ParseError",
    "TinyError",
    "TypeError_",
    "Position",
    "Span",
]
__version__ = "1.0.0"
