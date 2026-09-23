"""手写词法分析器。

Token 类型一览见 TokenType。词法规则在 docs/LANGUAGE.md 中文档化，
要点：
- 标识符 [A-Za-z_][A-Za-z0-9_]*
- 整数     [0-9]+（编译期检查 i16 范围）
- 关键字   fn/var/if/else/while/return/print/true/false
- 注释     // 行注释；/* ... */ 块注释（不嵌套）
- 多字符运算符优先匹配：== != <= >= && ||
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum, auto

from .errors import CompileError, Diagnostic
from .location import SourceText, Span


class TokenType(Enum):
    # 字面量 / 名字
    INT = auto()
    IDENT = auto()
    # 关键字
    FN = auto()
    VAR = auto()
    IF = auto()
    ELSE = auto()
    WHILE = auto()
    RETURN = auto()
    PRINT = auto()
    TRUE = auto()
    FALSE = auto()
    # 标点
    LPAREN = auto()
    RPAREN = auto()
    LBRACE = auto()
    RBRACE = auto()
    COMMA = auto()
    COLON = auto()
    SEMI = auto()
    # 运算符
    ASSIGN = auto()       # =
    EQ = auto()           # ==
    NE = auto()           # !=
    LT = auto()
    GT = auto()
    LE = auto()
    GE = auto()
    PLUS = auto()
    MINUS = auto()
    STAR = auto()
    SLASH = auto()
    PERCENT = auto()
    BANG = auto()         # !
    AND = auto()          # &&
    OR = auto()           # ||
    EOF = auto()


KEYWORDS: dict[str, TokenType] = {
    "fn": TokenType.FN,
    "var": TokenType.VAR,
    "if": TokenType.IF,
    "else": TokenType.ELSE,
    "while": TokenType.WHILE,
    "return": TokenType.RETURN,
    "print": TokenType.PRINT,
    "true": TokenType.TRUE,
    "false": TokenType.FALSE,
}

_SIMPLE: dict[str, TokenType] = {
    "(": TokenType.LPAREN,
    ")": TokenType.RPAREN,
    "{": TokenType.LBRACE,
    "}": TokenType.RBRACE,
    ",": TokenType.COMMA,
    ":": TokenType.COLON,
    ";": TokenType.SEMI,
    "+": TokenType.PLUS,
    "-": TokenType.MINUS,
    "*": TokenType.STAR,
    "/": TokenType.SLASH,
    "%": TokenType.PERCENT,
}


@dataclass
class Token:
    type: TokenType
    value: str
    span: Span

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return f"Token({self.type.name}, {self.value!r}, {self.span.short()})"


class Lexer:
    def __init__(self, source: SourceText) -> None:
        self.src = source
        self.text = source.text
        self.pos = 0
        self.diags: list[Diagnostic] = []

    # --- 基础工具 ---
    def _peek(self, off: int = 0) -> str:
        i = self.pos + off
        return self.text[i] if i < len(self.text) else ""

    def _advance(self) -> str:
        ch = self.text[self.pos]
        self.pos += 1
        return ch

    def _skip_trivia(self) -> None:
        while self.pos < len(self.text):
            ch = self._peek()
            if ch in " \t\r\n":
                self.pos += 1
            elif ch == "/" and self._peek(1) == "/":
                while self.pos < len(self.text) and self._peek() != "\n":
                    self.pos += 1
            elif ch == "/" and self._peek(1) == "*":
                start = self.pos
                self.pos += 2
                closed = False
                while self.pos < len(self.text):
                    if self._peek() == "*" and self._peek(1) == "/":
                        self.pos += 2
                        closed = True
                        break
                    self.pos += 1
                if not closed:
                    self.diags.append(Diagnostic(
                        "未闭合的块注释 /* ... */",
                        self.src.span(start, min(start + 2, len(self.text))),
                    ))
            else:
                break

    def tokenize(self) -> list[Token]:
        tokens: list[Token] = []
        while True:
            self._skip_trivia()
            if self.pos >= len(self.text):
                break
            tok = self._next_token()
            if tok is not None:
                tokens.append(tok)
        if self.diags:
            raise CompileError(self.diags)
        eof_pos = len(self.text)
        tokens.append(Token(TokenType.EOF, "", self.src.span(eof_pos, eof_pos)))
        return tokens

    def _next_token(self) -> Token | None:
        start = self.pos
        ch = self._advance()

        if ch.isdigit():
            while self._peek().isdigit():
                self.pos += 1
            # 数字后紧跟标识符字符，如 123abc，报错
            if self._peek().isalpha() or self._peek() == "_":
                bad_end = self.pos
                while self._peek().isalnum() or self._peek() == "_":
                    self.pos += 1
                raise CompileError([Diagnostic(
                    f"数字字面量非法: {self.text[start:self.pos]!r}",
                    self.src.span(start, self.pos),
                )])
            return Token(TokenType.INT, self.text[start:self.pos], self.src.span(start, self.pos))

        if ch.isalpha() or ch == "_":
            while self._peek().isalnum() or self._peek() == "_":
                self.pos += 1
            word = self.text[start:self.pos]
            ttype = KEYWORDS.get(word, TokenType.IDENT)
            return Token(ttype, word, self.src.span(start, self.pos))

        if ch in _SIMPLE:
            return Token(_SIMPLE[ch], ch, self.src.span(start, self.pos))

        if ch == "=":
            if self._peek() == "=":
                self.pos += 1
                return Token(TokenType.EQ, "==", self.src.span(start, self.pos))
            return Token(TokenType.ASSIGN, "=", self.src.span(start, self.pos))
        if ch == "!":
            if self._peek() == "=":
                self.pos += 1
                return Token(TokenType.NE, "!=", self.src.span(start, self.pos))
            return Token(TokenType.BANG, "!", self.src.span(start, self.pos))
        if ch == "<":
            if self._peek() == "=":
                self.pos += 1
                return Token(TokenType.LE, "<=", self.src.span(start, self.pos))
            return Token(TokenType.LT, "<", self.src.span(start, self.pos))
        if ch == ">":
            if self._peek() == "=":
                self.pos += 1
                return Token(TokenType.GE, ">=", self.src.span(start, self.pos))
            return Token(TokenType.GT, ">", self.src.span(start, self.pos))
        if ch == "&":
            if self._peek() == "&":
                self.pos += 1
                return Token(TokenType.AND, "&&", self.src.span(start, self.pos))
            raise CompileError([Diagnostic(
                "孤立的 '&'，是否想写 '&&' ?",
                self.src.span(start, self.pos),
            )])
        if ch == "|":
            if self._peek() == "|":
                self.pos += 1
                return Token(TokenType.OR, "||", self.src.span(start, self.pos))
            raise CompileError([Diagnostic(
                "孤立的 '|'，是否想写 '||' ?",
                self.src.span(start, self.pos),
            )])

        raise CompileError([Diagnostic(
            f"无法识别的字符 {ch!r}",
            self.src.span(start, self.pos),
        )])


def tokenize(source: SourceText) -> list[Token]:
    return Lexer(source).tokenize()
