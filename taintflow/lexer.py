"""手写词法分析器。

不使用正则（即便使用标准库 re 也不属于“现成编译器”，这里仍逐字符扫描以便
精确控制位置与错误）。产生的每个 Token 都带 :class:`~taintflow.location.Span`。

支持的词法单元见 README“语言定义”。字符串仅支持 ' ' 与 " "，转义包括
``\\n \\t \\r \\" \\' \\\\``。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import List

from .errors import LexError
from .location import Pos, Span

KEYWORDS = frozenset(
    {"fn", "if", "else", "while", "return", "true", "false", "and", "or", "not"}
)

# 多字符操作符必须排在其前缀之前
_OPERATORS = ("==", "!=", "<=", ">=", "&&", "||", "!", "<", ">", "+", "-", "*", "/", "%", "=")
# 所有操作符（含其首字符前缀，使 && 的第二个 & 等也进入操作符扫描分支）
PUNCTUATORS = frozenset("(){},;") | frozenset(_OPERATORS) | frozenset("&|=")


@dataclass(frozen=True)
class Token:
    kind: str  # 'ident' | 'keyword' | 'number' | 'string' | 'op' | 'eof'
    value: object
    span: Span

    @property
    def line(self) -> int:
        return self.span.start.line


class Lexer:
    def __init__(self, src: str, filename: str = "<input>"):
        self.src = src
        self.filename = filename
        self.n = len(src)
        self.i = 0
        self.line = 1
        self.col = 1

    # ---- 位置簿记 ----
    def _pos(self) -> Pos:
        return Pos(self.i, self.line, self.col)

    def _span(self, start: Pos) -> Span:
        return Span(start, self._pos())

    def _peek(self, off: int = 0) -> str:
        j = self.i + off
        return self.src[j] if j < self.n else ""

    def _advance(self) -> str:
        ch = self.src[self.i]
        self.i += 1
        if ch == "\n":
            self.line += 1
            self.col = 1
        else:
            self.col += 1
        return ch

    # ---- 主循环 ----
    def tokenize(self) -> List[Token]:
        tokens: List[Token] = []
        while self.i < self.n:
            ch = self._peek()
            if ch in " \t\r\n":
                self._advance()
                continue
            if ch == "/" and self._peek(1) == "/":
                while self.i < self.n and self._peek() != "\n":
                    self._advance()
                continue
            if ch == "/" and self._peek(1) == "*":
                self._read_block_comment()
                continue
            start = self._pos()
            if ch.isalpha() or ch == "_":
                tokens.append(self._read_ident(start))
            elif ch.isdigit():
                tokens.append(self._read_number(start))
            elif ch in ("'", '"'):
                tokens.append(self._read_string(start))
            elif ch in PUNCTUATORS:
                tokens.append(self._read_operator(start))
            else:
                raise LexError(f"无法识别的字符 {ch!r}", self._span(start))
        tokens.append(Token("eof", None, Span(self._pos(), self._pos())))
        return tokens

    def _read_block_comment(self) -> None:
        start = self._pos()
        self._advance()
        self._advance()
        depth = 1
        while self.i < self.n and depth > 0:
            if self._peek() == "/" and self._peek(1) == "*":
                self._advance()
                self._advance()
                depth += 1
            elif self._peek() == "*" and self._peek(1) == "/":
                self._advance()
                self._advance()
                depth -= 1
            else:
                self._advance()
        if depth > 0:
            raise LexError("块注释未闭合", Span(start, self._pos()))

    def _read_ident(self, start: Pos) -> Token:
        while self.i < self.n and (self._peek().isalnum() or self._peek() == "_"):
            self._advance()
        text = self.src[start.offset : self.i]
        kind = "keyword" if text in KEYWORDS else "ident"
        return Token(kind, text, self._span(start))

    def _read_number(self, start: Pos) -> Token:
        while self.i < self.n and self._peek().isdigit():
            self._advance()
        text = self.src[start.offset : self.i]
        return Token("number", int(text), self._span(start))

    def _read_string(self, start: Pos) -> Token:
        quote = self._advance()
        chars: List[str] = []
        while self.i < self.n and self._peek() != quote:
            ch = self._advance()
            if ch == "\\":
                if self.i >= self.n:
                    break
                esc = self._advance()
                chars.append(
                    {"n": "\n", "t": "\t", "r": "\r", '"': '"', "'": "'", "\\": "\\"}.get(
                        esc, esc
                    )
                )
            elif ch == "\n":
                raise LexError("字符串中不允许出现换行", self._span(start))
            else:
                chars.append(ch)
        if self.i >= self.n:
            raise LexError("字符串未闭合", self._span(start))
        self._advance()  # 右引号
        return Token("string", "".join(chars), self._span(start))

    def _read_operator(self, start: Pos) -> Token:
        for op in _OPERATORS:
            if self.src.startswith(op, self.i):
                for _ in op:
                    self._advance()
                return Token("op", op, self._span(start))
        # 单个标点
        ch = self._advance()
        return Token("op", ch, self._span(start))
