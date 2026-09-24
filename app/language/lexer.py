"""Lexer for the small scripting language (see README.md for the grammar).

The lexer is deliberately hand-written (no third-party parsing dependency) so
that the whole analysis pipeline stays auditable.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum, auto


class TokenType(Enum):
    # literals / names
    INTEGER = auto()
    STRING = auto()
    TRUE = auto()
    FALSE = auto()
    NAME = auto()
    # keywords
    FUNC = auto()
    RETURN = auto()
    IF = auto()
    ELSE = auto()
    WHILE = auto()
    VAR = auto()
    NIL = auto()
    AND = auto()
    OR = auto()
    NOT = auto()
    # punctuation
    LPAREN = auto()
    RPAREN = auto()
    LBRACE = auto()
    RBRACE = auto()
    COMMA = auto()
    SEMI = auto()
    ASSIGN = auto()
    # operators
    PLUS = auto()
    MINUS = auto()
    STAR = auto()
    SLASH = auto()
    PERCENT = auto()
    EQ = auto()
    NE = auto()
    LT = auto()
    LE = auto()
    GT = auto()
    GE = auto()
    BANG = auto()
    EOF = auto()


KEYWORDS = {
    "func": TokenType.FUNC,
    "return": TokenType.RETURN,
    "if": TokenType.IF,
    "else": TokenType.ELSE,
    "while": TokenType.WHILE,
    "var": TokenType.VAR,
    "nil": TokenType.NIL,
    "true": TokenType.TRUE,
    "false": TokenType.FALSE,
    "and": TokenType.AND,
    "or": TokenType.OR,
    "not": TokenType.NOT,
}


@dataclass(frozen=True)
class Token:
    type: TokenType
    value: str
    line: int
    col: int


class LexError(Exception):
    def __init__(self, message: str, line: int, col: int):
        super().__init__(f"{message} at line {line}, column {col}")
        self.line = line
        self.col = col


_SIMPLE = {
    "(": TokenType.LPAREN,
    ")": TokenType.RPAREN,
    "{": TokenType.LBRACE,
    "}": TokenType.RBRACE,
    ",": TokenType.COMMA,
    ";": TokenType.SEMI,
    "+": TokenType.PLUS,
    "-": TokenType.MINUS,
    "*": TokenType.STAR,
    "/": TokenType.SLASH,
    "%": TokenType.PERCENT,
}


class Lexer:
    def __init__(self, source: str):
        self.src = source
        self.n = len(source)
        self.pos = 0
        self.line = 1
        self.col = 1

    # -- low-level helpers -------------------------------------------------
    def _peek(self, offset: int = 0) -> str:
        p = self.pos + offset
        return self.src[p] if p < self.n else ""

    def _advance(self) -> str:
        ch = self.src[self.pos]
        self.pos += 1
        if ch == "\n":
            self.line += 1
            self.col = 1
        else:
            self.col += 1
        return ch

    def _skip_trivia(self) -> None:
        while self.pos < self.n:
            ch = self._peek()
            if ch in " \t\r\n":
                self._advance()
            elif ch == "/" and self._peek(1) == "/":
                while self.pos < self.n and self._peek() != "\n":
                    self._advance()
            elif ch == "/" and self._peek(1) == "*":
                start_line, start_col = self.line, self.col
                self._advance()
                self._advance()
                while self.pos < self.n and not (
                    self._peek() == "*" and self._peek(1) == "/"
                ):
                    self._advance()
                if self.pos >= self.n:
                    raise LexError("unterminated block comment", start_line, start_col)
                self._advance()
                self._advance()
            else:
                return

    def _read_string(self, quote: str, line: int, col: int) -> Token:
        self._advance()  # opening quote
        chars: list[str] = []
        while self.pos < self.n and self._peek() != quote:
            ch = self._peek()
            if ch == "\n":
                raise LexError("unterminated string", line, col)
            if ch == "\\":
                self._advance()
                esc = self._peek()
                escapes = {"n": "\n", "t": "\t", "r": "\r", "\\": "\\",
                           "'": "'", '"': '"', "0": "\0"}
                if esc not in escapes:
                    raise LexError(f"invalid escape \\{esc}", self.line, self.col)
                chars.append(escapes[esc])
                self._advance()
            else:
                chars.append(self._advance())
        if self.pos >= self.n:
            raise LexError("unterminated string", line, col)
        self._advance()  # closing quote
        return Token(TokenType.STRING, "".join(chars), line, col)

    # -- main --------------------------------------------------------------
    def tokenize(self) -> list[Token]:
        tokens: list[Token] = []
        while True:
            self._skip_trivia()
            if self.pos >= self.n:
                tokens.append(Token(TokenType.EOF, "", self.line, self.col))
                return tokens
            line, col = self.line, self.col
            ch = self._peek()

            if ch.isdigit():
                start = self.pos
                while self._peek().isdigit():
                    self._advance()
                tokens.append(Token(TokenType.INTEGER, self.src[start:self.pos], line, col))
                continue
            if ch.isalpha() or ch == "_":
                start = self.pos
                while self._peek().isalnum() or self._peek() == "_":
                    self._advance()
                word = self.src[start:self.pos]
                ttype = KEYWORDS.get(word, TokenType.NAME)
                tokens.append(Token(ttype, word, line, col))
                continue
            if ch in ("'", '"'):
                tokens.append(self._read_string(ch, line, col))
                continue
            if ch == "=":
                self._advance()
                if self._peek() == "=":
                    self._advance()
                    tokens.append(Token(TokenType.EQ, "==", line, col))
                else:
                    tokens.append(Token(TokenType.ASSIGN, "=", line, col))
                continue
            if ch == "!":
                self._advance()
                if self._peek() == "=":
                    self._advance()
                    tokens.append(Token(TokenType.NE, "!=", line, col))
                else:
                    tokens.append(Token(TokenType.BANG, "!", line, col))
                continue
            if ch == "<":
                self._advance()
                if self._peek() == "=":
                    self._advance()
                    tokens.append(Token(TokenType.LE, "<=", line, col))
                else:
                    tokens.append(Token(TokenType.LT, "<", line, col))
                continue
            if ch == ">":
                self._advance()
                if self._peek() == "=":
                    self._advance()
                    tokens.append(Token(TokenType.GE, ">=", line, col))
                else:
                    tokens.append(Token(TokenType.GT, ">", line, col))
                continue
            if ch in _SIMPLE:
                self._advance()
                tokens.append(Token(_SIMPLE[ch], ch, line, col))
                continue

            raise LexError(f"unexpected character {ch!r}", line, col)
