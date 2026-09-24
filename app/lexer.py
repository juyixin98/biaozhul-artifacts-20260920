"""Lexer for the small scripting language (TaintLang).

The language is intentionally tiny -- see README.md for the grammar --
but covers assignment, branching, loops, first-order functions and the
preset builtins source() / sanitize() / sink().
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum, auto


class TokenType(Enum):
    # literals / names
    INT = auto()
    STRING = auto()
    NAME = auto()
    # keywords
    FUNC = auto()
    IF = auto()
    ELSE = auto()
    WHILE = auto()
    RETURN = auto()
    TRUE = auto()
    FALSE = auto()
    NIL = auto()
    # punctuation
    LPAREN = auto()
    RPAREN = auto()
    LBRACE = auto()
    RBRACE = auto()
    COMMA = auto()
    SEMI = auto()
    ASSIGN = auto()  # =
    PLUS = auto()
    MINUS = auto()
    STAR = auto()
    SLASH = auto()
    PERCENT = auto()
    EQ = auto()
    NEQ = auto()
    LT = auto()
    LE = auto()
    GT = auto()
    GE = auto()
    AND = auto()
    OR = auto()
    NOT = auto()
    EOF = auto()


KEYWORDS = {
    "func": TokenType.FUNC,
    "if": TokenType.IF,
    "else": TokenType.ELSE,
    "while": TokenType.WHILE,
    "return": TokenType.RETURN,
    "true": TokenType.TRUE,
    "false": TokenType.FALSE,
    "nil": TokenType.NIL,
}


@dataclass(frozen=True)
class Loc:
    line: int
    col: int

    def __str__(self) -> str:
        return f"{self.line}:{self.col}"


@dataclass(frozen=True)
class Token:
    type: TokenType
    value: str
    loc: Loc


class LexError(Exception):
    def __init__(self, message: str, loc: Loc):
        super().__init__(f"{message} at line {loc.line}, col {loc.col}")
        self.loc = loc


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
        self.i = 0
        self.line = 1
        self.col = 1

    def _peek(self, offset: int = 0) -> str:
        j = self.i + offset
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

    def tokenize(self) -> list[Token]:
        tokens: list[Token] = []
        while self.i < self.n:
            ch = self._peek()
            if ch in " \t\r\n":
                self._advance()
                continue
            if ch == "#":  # line comment
                while self.i < self.n and self._peek() != "\n":
                    self._advance()
                continue
            loc = Loc(self.line, self.col)
            if ch.isalpha() or ch == "_":
                tokens.append(self._read_name(loc))
                continue
            if ch.isdigit():
                tokens.append(self._read_int(loc))
                continue
            if ch == '"':
                tokens.append(self._read_string(loc))
                continue
            if ch in _SIMPLE:
                self._advance()
                tokens.append(Token(_SIMPLE[ch], ch, loc))
                continue
            if ch == "=":
                self._advance()
                if self._peek() == "=":
                    self._advance()
                    tokens.append(Token(TokenType.EQ, "==", loc))
                else:
                    tokens.append(Token(TokenType.ASSIGN, "=", loc))
                continue
            if ch == "!":
                self._advance()
                if self._peek() == "=":
                    self._advance()
                    tokens.append(Token(TokenType.NEQ, "!=", loc))
                else:
                    tokens.append(Token(TokenType.NOT, "!", loc))
                continue
            if ch == "<":
                self._advance()
                if self._peek() == "=":
                    self._advance()
                    tokens.append(Token(TokenType.LE, "<=", loc))
                else:
                    tokens.append(Token(TokenType.LT, "<", loc))
                continue
            if ch == ">":
                self._advance()
                if self._peek() == "=":
                    self._advance()
                    tokens.append(Token(TokenType.GE, ">=", loc))
                else:
                    tokens.append(Token(TokenType.GT, ">", loc))
                continue
            if ch == "&":
                self._advance()
                if self._peek() == "&":
                    self._advance()
                    tokens.append(Token(TokenType.AND, "&&", loc))
                    continue
                raise LexError("unexpected '&'; did you mean '&&'?", loc)
            if ch == "|":
                self._advance()
                if self._peek() == "|":
                    self._advance()
                    tokens.append(Token(TokenType.OR, "||", loc))
                    continue
                raise LexError("unexpected '|'; did you mean '||'?", loc)
            raise LexError(f"unexpected character {ch!r}", loc)
        tokens.append(Token(TokenType.EOF, "", Loc(self.line, self.col)))
        return tokens

    def _read_name(self, loc: Loc) -> Token:
        start = self.i
        while self.i < self.n and (self._peek().isalnum() or self._peek() == "_"):
            self._advance()
        value = self.src[start:self.i]
        ttype = KEYWORDS.get(value, TokenType.NAME)
        return Token(ttype, value, loc)

    def _read_int(self, loc: Loc) -> Token:
        start = self.i
        while self.i < self.n and self._peek().isdigit():
            self._advance()
        return Token(TokenType.INT, self.src[start:self.i], loc)

    def _read_string(self, loc: Loc) -> Token:
        self._advance()  # opening quote
        chars: list[str] = []
        escapes = {"n": "\n", "t": "\t", '"': '"', "\\": "\\", "0": "\0"}
        while self.i < self.n and self._peek() != '"':
            ch = self._advance()
            if ch == "\n":
                raise LexError("unterminated string literal", loc)
            if ch == "\\":
                if self.i >= self.n:
                    break
                nxt = self._advance()
                if nxt not in escapes:
                    raise LexError(f"invalid string escape \\{nxt}", loc)
                chars.append(escapes[nxt])
            else:
                chars.append(ch)
        if self.i >= self.n:
            raise LexError("unterminated string literal", loc)
        self._advance()  # closing quote
        return Token(TokenType.STRING, "".join(chars), loc)
