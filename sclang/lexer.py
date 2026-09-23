"""Hand-written lexer for ScL.

No external scanner/generator is used: every token is produced by this
module and carries its :class:`~sclang.errors.Span`.
"""

from dataclasses import dataclass
from enum import Enum, auto

from .errors import CompileError, Span


class TokenKind(Enum):
    # literals / names
    INT = auto()
    STRING = auto()
    IDENT = auto()
    # keywords
    LET = auto()
    FN = auto()
    IF = auto()
    ELSE = auto()
    WHILE = auto()
    RETURN = auto()
    TRUE = auto()
    FALSE = auto()
    NIL = auto()
    PRINT = auto()
    AND = auto()
    OR = auto()
    NOT = auto()
    # punctuation
    LPAREN = auto()
    RPAREN = auto()
    LBRACE = auto()
    RBRACE = auto()
    COMMA = auto()
    SEMICOLON = auto()
    ASSIGN = auto()
    EQ = auto()
    NE = auto()
    LT = auto()
    LE = auto()
    GT = auto()
    GE = auto()
    PLUS = auto()
    MINUS = auto()
    STAR = auto()
    SLASH = auto()
    PERCENT = auto()
    EOF = auto()


KEYWORDS = {
    "let": TokenKind.LET,
    "fn": TokenKind.FN,
    "if": TokenKind.IF,
    "else": TokenKind.ELSE,
    "while": TokenKind.WHILE,
    "return": TokenKind.RETURN,
    "true": TokenKind.TRUE,
    "false": TokenKind.FALSE,
    "nil": TokenKind.NIL,
    "print": TokenKind.PRINT,
    "and": TokenKind.AND,
    "or": TokenKind.OR,
    "not": TokenKind.NOT,
}

# simple one- / two-character punctuation
PUNCT = {
    "(": TokenKind.LPAREN,
    ")": TokenKind.RPAREN,
    "{": TokenKind.LBRACE,
    "}": TokenKind.RBRACE,
    ",": TokenKind.COMMA,
    ";": TokenKind.SEMICOLON,
    "=": TokenKind.ASSIGN,
    "==": TokenKind.EQ,
    "!=": TokenKind.NE,
    "<": TokenKind.LT,
    "<=": TokenKind.LE,
    ">": TokenKind.GT,
    ">=": TokenKind.GE,
    "+": TokenKind.PLUS,
    "-": TokenKind.MINUS,
    "*": TokenKind.STAR,
    "/": TokenKind.SLASH,
    "%": TokenKind.PERCENT,
}


@dataclass
class Token:
    kind: TokenKind
    value: object
    span: Span


class Lexer:
    def __init__(self, source: str, filename: str = "<input>"):
        self.source = source
        self.filename = filename
        self.pos = 0
        self.line = 1
        self.col = 1

    # -- position helpers -------------------------------------------------

    def _span(self, start: int, sl: int, sc: int) -> Span:
        return Span(
            start=start,
            end=self.pos,
            line=sl,
            col=sc,
            end_line=self.line,
            end_col=self.col,
            source=self.source,
        )

    def _peek(self, off: int = 0) -> str:
        i = self.pos + off
        return self.source[i] if i < len(self.source) else ""

    def _advance(self) -> str:
        ch = self.source[self.pos]
        self.pos += 1
        if ch == "\n":
            self.line += 1
            self.col = 1
        else:
            self.col += 1
        return ch

    # -- entry point ------------------------------------------------------

    def tokenize(self) -> list[Token]:
        tokens: list[Token] = []
        while self.pos < len(self.source):
            ch = self._peek()
            if ch in " \t\r\n":
                self._advance()
                continue
            if ch == "#":  # line comment to end of line
                while self.pos < len(self.source) and self._peek() != "\n":
                    self._advance()
                continue
            sl, sc, start = self.line, self.col, self.pos
            if ch.isdigit():
                tokens.append(self._number(start, sl, sc))
            elif ch == "_" or ch.isalpha():
                tokens.append(self._ident(start, sl, sc))
            elif ch == '"':
                tokens.append(self._string(start, sl, sc))
            else:
                tokens.append(self._punct(start, sl, sc))
        tokens.append(
            Token(
                TokenKind.EOF,
                None,
                Span(self.pos, self.pos, self.line, self.col, self.line, self.col,
                     self.source),
            )
        )
        return tokens

    # -- token scanners ---------------------------------------------------

    def _number(self, start: int, sl: int, sc: int) -> Token:
        while self._peek().isdigit():
            self._advance()
        text = self.source[start:self.pos]
        return Token(TokenKind.INT, int(text), self._span(start, sl, sc))

    def _ident(self, start: int, sl: int, sc: int) -> Token:
        ch = self._peek()
        while ch == "_" or ch.isalnum():
            self._advance()
            ch = self._peek()
        text = self.source[start:self.pos]
        kind = KEYWORDS.get(text, TokenKind.IDENT)
        value = text if kind in (TokenKind.IDENT, TokenKind.PRINT) else None
        return Token(kind, value, self._span(start, sl, sc))

    def _string(self, start: int, sl: int, sc: int) -> Token:
        self._advance()  # opening quote
        out: list[str] = []
        escapes = {"n": "\n", "t": "\t", '"': '"', "\\": "\\", "r": "\r", "0": "\0"}
        while True:
            if self.pos >= len(self.source):
                raise CompileError("unterminated string literal",
                                   self._span(start, sl, sc))
            ch = self._peek()
            if ch == '"':
                self._advance()
                break
            if ch == "\n":
                raise CompileError("newline in string literal",
                                   self._span(start, sl, sc))
            if ch == "\\":
                self._advance()
                esc = self._peek()
                if esc not in escapes:
                    raise CompileError(f"invalid string escape \\{esc}",
                                       self._span(start, sl, sc))
                out.append(escapes[esc])
                self._advance()
            else:
                out.append(self._advance())
        return Token(TokenKind.STRING, "".join(out), self._span(start, sl, sc))

    def _punct(self, start: int, sl: int, sc: int) -> Token:
        two = self.source[self.pos:self.pos + 2]
        if len(two) == 2 and two in PUNCT:
            self._advance()
            self._advance()
            return Token(PUNCT[two], two, self._span(start, sl, sc))
        one = self._peek()
        if one in PUNCT:
            self._advance()
            return Token(PUNCT[one], one, self._span(start, sl, sc))
        raise CompileError(f"unexpected character {one!r}",
                           self._span(start, sl, sc))
