"""手写词法分析器。

将 VLang 源码切分为带源码位置（:class:`~byteverifier.common.Span`）的 token 流。
不依赖任何外部编译器/解析器库。
"""

from __future__ import annotations

from dataclasses import dataclass

from .common import PHASE_LEXER, Span, ToolError

# token 类型
T_INT = "INT"          # 整数字面量
T_IDENT = "IDENT"
T_KEYWORD = "KEYWORD"
T_PUNCT = "PUNCT"
T_EOF = "EOF"

KEYWORDS = {
    "int", "bool", "void", "true", "false",
    "if", "else", "while", "return",
}

PUNCT_2 = {"==", "!=", "<=", ">=", "&&", "||"}
PUNCT_1 = set("+-*/(){};,=!<>")


@dataclass(frozen=True)
class Token:
    kind: str
    value: str
    span: Span
    int_value: int = 0


class Lexer:
    def __init__(self, source: str, filename: str = "<input>") -> None:
        self.src = source
        self.fn = filename
        self.n = len(source)
        self.i = 0
        self.line = 1
        self.col = 1

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

    def _skip_ws_comments(self) -> None:
        while self.i < self.n:
            ch = self._peek()
            if ch in " \t\r\n":
                self._advance()
            elif ch == "/" and self._peek(1) == "/":
                while self.i < self.n and self._peek() != "\n":
                    self._advance()
            elif ch == "/" and self._peek(1) == "*":
                self._advance(); self._advance()
                while self.i < self.n and not (
                    self._peek() == "*" and self._peek(1) == "/"
                ):
                    self._advance()
                if self.i < self.n:
                    self._advance(); self._advance()
            else:
                return

    def tokenize(self) -> list[Token]:
        tokens: list[Token] = []
        while True:
            self._skip_ws_comments()
            if self.i >= self.n:
                break
            line, col = self.line, self.col
            ch = self._peek()

            if ch.isdigit():
                start = self.i
                while self._peek().isdigit():
                    self._advance()
                text = self.src[start:self.i]
                try:
                    val = int(text)
                except ValueError:
                    raise ToolError(
                        PHASE_LEXER, "integer.literal",
                        f"整数字面量超出范围: {text}",
                        Span(self.fn, line, col),
                    )
                tokens.append(Token(
                    T_INT, text, Span(self.fn, line, col, line, col + len(text) - 1),
                    int_value=val,
                ))
                continue

            if ch.isalpha() or ch == "_":
                start = self.i
                while self._peek().isalnum() or self._peek() == "_":
                    self._advance()
                text = self.src[start:self.i]
                kind = T_KEYWORD if text in KEYWORDS else T_IDENT
                tokens.append(Token(
                    kind, text,
                    Span(self.fn, line, col, line, col + len(text) - 1),
                ))
                continue

            two = self.src[self.i:self.i + 2]
            if two in PUNCT_2:
                self._advance(); self._advance()
                tokens.append(Token(T_PUNCT, two, Span(self.fn, line, col, line, col + 1)))
                continue
            if ch in PUNCT_1:
                self._advance()
                tokens.append(Token(T_PUNCT, ch, Span(self.fn, line, col)))
                continue

            raise ToolError(
                PHASE_LEXER, "unexpected.char",
                f"无法识别的字符: {ch!r}",
                Span(self.fn, line, col),
            )

        tokens.append(Token(T_EOF, "", Span(self.fn, self.line, self.col)))
        return tokens
