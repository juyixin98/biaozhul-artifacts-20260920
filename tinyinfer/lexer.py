"""手写词法分析器。

不使用 re 之外的扫描框架（实际上连 re 也不用）：逐字符消费，
每个 Token 都记录精确的 :class:`~tinyinfer.locations.Span`。

支持：
  - 整数、标识符、关键字
  - 双字符运算符  == != <= >= := <-
  - 行注释 //... 与块注释 (* ... *)（可嵌套）
"""
from __future__ import annotations

from dataclasses import dataclass

from .errors import LexError
from .locations import Position, Span

KEYWORDS = {
    "let", "rec", "in", "fun", "if", "then", "else",
    "true", "false", "ref", "deref", "unit",
}
# not 不是关键字：它是普通的内建多态函数（标识符），从而既可以
# `not x` 调用，也可以把 not 本身作为值传递（如 twice not）。


@dataclass(frozen=True)
class Token:
    kind: str          # 运算符原样存放（如 "(" "+" "->"），标识符为 "IDENT"
    value: str
    span: Span

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return f"Token({self.kind!r}, {self.value!r})"


TWO_CHAR_OPS = {"==", "!=", "<=", ">=", ":=", "<-", "->"}
SINGLE_CHAR_OPS = set("()+-.*/<>=;,:")


class Lexer:
    def __init__(self, source: str, filename: str = "<input>"):
        self.src = source
        self.file = filename
        self.n = len(source)
        self.i = 0
        self.line = 1
        self.col = 1

    # ---- 位置工具 -------------------------------------------------------
    def _pos(self) -> Position:
        return Position(self.i, self.line, self.col)

    def _span(self, start: Position) -> Span:
        return Span(start, Position(self.i, self.line, self.col), self.file)

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

    # ---- 跳过空白与注释 --------------------------------------------------
    def _skip_trivia(self) -> None:
        while self.i < self.n:
            ch = self._peek()
            if ch in " \t\r\n":
                self._advance()
            elif ch == "/" and self._peek(1) == "/":
                while self.i < self.n and self._peek() != "\n":
                    self._advance()
            elif ch == "(" and self._peek(1) == "*":
                self._block_comment()
            else:
                return

    def _block_comment(self) -> None:
        start = self._pos()
        depth = 0
        while self.i < self.n:
            if self._peek() == "(" and self._peek(1) == "*":
                self._advance()
                self._advance()
                depth += 1
            elif self._peek() == "*" and self._peek(1) == ")":
                self._advance()
                self._advance()
                depth -= 1
                if depth == 0:
                    return
            else:
                self._advance()
        raise LexError("块注释未终止（缺少 *)）", self._span(start))

    # ---- 主入口 ----------------------------------------------------------
    def tokenize(self) -> list[Token]:
        tokens: list[Token] = []
        while True:
            self._skip_trivia()
            if self.i >= self.n:
                break
            start = self._pos()
            ch = self._peek()

            if ch.isdigit():
                tokens.append(self._number(start))
            elif ch.isalpha() or ch == "_":
                tokens.append(self._ident(start))
            else:
                two = self.src[self.i:self.i + 2]
                if two in TWO_CHAR_OPS:
                    self._advance()
                    self._advance()
                    tokens.append(Token(two, two, self._span(start)))
                elif ch in SINGLE_CHAR_OPS:
                    self._advance()
                    tokens.append(Token(ch, ch, self._span(start)))
                else:
                    self._advance()
                    raise LexError(f"非法字符 {ch!r}", self._span(start))
        return tokens

    def _number(self, start: Position) -> Token:
        while self.i < self.n and self._peek().isdigit():
            self._advance()
        # 数字后紧跟字母视为非法，避免把 123abc 当成两个 token
        if self.i < self.n and (self._peek().isalpha() or self._peek() == "_"):
            bad = self._pos()
            self._advance()
            raise LexError("数字字面量格式错误", Span(bad, self._pos(), self.file))
        text = self.src[start.offset:self.i]
        return Token("INT", text, self._span(start))

    def _ident(self, start: Position) -> Token:
        while self.i < self.n and (
            self._peek().isalnum() or self._peek() == "_"
        ):
            self._advance()
        text = self.src[start.offset:self.i]
        kind = text if text in KEYWORDS else "IDENT"
        return Token(kind, text, self._span(start))
