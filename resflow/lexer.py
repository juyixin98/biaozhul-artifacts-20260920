"""Hand-written lexer for the resflow language.

No compiler generator or third-party library is used: every token is produced
by explicit scanning rules below.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum, auto
from typing import List

from .errors import LexError
from .locations import SourceFile, Span


class TokenType(Enum):
    # Literals / names
    IDENT = auto()
    INT = auto()
    STRING = auto()
    # Keywords
    FUN = auto()
    LET = auto()
    ACQUIRE = auto()
    RELEASE = auto()
    USE = auto()
    RETURN = auto()
    THROW = auto()
    IF = auto()
    ELSE = auto()
    WHILE = auto()
    TRY = auto()
    CATCH = auto()
    TRUE = auto()
    FALSE = auto()
    NULL = auto()
    THROWS = auto()
    # Punctuation
    LPAREN = auto()
    RPAREN = auto()
    LBRACE = auto()
    RBRACE = auto()
    COMMA = auto()
    SEMICOLON = auto()
    ASSIGN = auto()
    # Operators
    BANG = auto()
    AND = auto()
    OR = auto()
    EOF = auto()


KEYWORDS = {
    "fun": TokenType.FUN,
    "let": TokenType.LET,
    "acquire": TokenType.ACQUIRE,
    "release": TokenType.RELEASE,
    "use": TokenType.USE,
    "return": TokenType.RETURN,
    "throw": TokenType.THROW,
    "if": TokenType.IF,
    "else": TokenType.ELSE,
    "while": TokenType.WHILE,
    "try": TokenType.TRY,
    "catch": TokenType.CATCH,
    "true": TokenType.TRUE,
    "false": TokenType.FALSE,
    "null": TokenType.NULL,
    "throws": TokenType.THROWS,
}

# Multi-character punctuation, longest first.
PUNCT = {
    "(": TokenType.LPAREN,
    ")": TokenType.RPAREN,
    "{": TokenType.LBRACE,
    "}": TokenType.RBRACE,
    ",": TokenType.COMMA,
    ";": TokenType.SEMICOLON,
    "=": TokenType.ASSIGN,
    "!": TokenType.BANG,
    "&&": TokenType.AND,
    "||": TokenType.OR,
}


@dataclass(frozen=True)
class Token:
    type: TokenType
    text: str
    span: Span
    int_value: int = 0


class Lexer:
    def __init__(self, source: SourceFile):
        self.src = source
        self.text = source.text
        self.n = len(self.text)
        self.i = 0

    def tokenize(self) -> List[Token]:
        tokens: List[Token] = []
        while self.i < self.n:
            ch = self.text[self.i]
            if ch in " \t\r\n":
                self.i += 1
            elif ch == "/" and self.peek(1) == "/":
                self.skip_line_comment()
            elif ch.isalpha() or ch == "_":
                tokens.append(self.lex_ident())
            elif ch.isdigit():
                tokens.append(self.lex_int())
            elif ch == '"':
                tokens.append(self.lex_string())
            else:
                tokens.append(self.lex_punct())
        tokens.append(Token(TokenType.EOF, "", self.src.span(self.i, self.i)))
        return tokens

    def peek(self, ahead: int = 0) -> str:
        j = self.i + ahead
        return self.text[j] if j < self.n else ""

    def skip_line_comment(self) -> None:
        while self.i < self.n and self.text[self.i] != "\n":
            self.i += 1

    def lex_ident(self) -> Token:
        start = self.i
        while self.i < self.n and (self.text[self.i].isalnum() or self.text[self.i] == "_"):
            self.i += 1
        text = self.text[start:self.i]
        ttype = KEYWORDS.get(text, TokenType.IDENT)
        return Token(ttype, text, self.src.span(start, self.i))

    def lex_int(self) -> Token:
        start = self.i
        while self.i < self.n and self.text[self.i].isdigit():
            self.i += 1
        text = self.text[start:self.i]
        return Token(TokenType.INT, text, self.src.span(start, self.i), int_value=int(text))

    def lex_string(self) -> Token:
        start = self.i
        self.i += 1  # opening quote
        chars: List[str] = []
        while self.i < self.n and self.text[self.i] != '"':
            ch = self.text[self.i]
            if ch == "\n":
                raise LexError("unterminated string literal", self.src.span(start, self.i))
            if ch == "\\":
                self.i += 1
                if self.i >= self.n:
                    break
                esc = self.text[self.i]
                chars.append({"n": "\n", "t": "\t", '"': '"', "\\": "\\"}.get(esc, esc))
                self.i += 1
            else:
                chars.append(ch)
                self.i += 1
        if self.i >= self.n:
            raise LexError("unterminated string literal", self.src.span(start, self.i))
        self.i += 1  # closing quote
        return Token(TokenType.STRING, "".join(chars), self.src.span(start, self.i))

    def lex_punct(self) -> Token:
        start = self.i
        # Longest match: && and || before single characters.
        for length in (2, 1):
            candidate = self.text[self.i:self.i + length]
            ttype = PUNCT.get(candidate)
            if ttype is not None:
                self.i += length
                return Token(ttype, candidate, self.src.span(start, self.i))
        ch = self.text[self.i]
        raise LexError(f"unexpected character {ch!r}", self.src.span(start, start + 1))
