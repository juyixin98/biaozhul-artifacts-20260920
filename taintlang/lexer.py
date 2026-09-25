"""Hand-written lexer for TaintLang.

The core parsing pipeline (this file, ``parser`` and ``builder``) uses no
third-party parser/compiler generator: every token is scanned here and its
exact source span is retained.

Token grammar::

    NUMBER   = [0-9]+
    STRING   = '"' (~["\\] | '\\' .)* '"'   (no escapes are interpreted;
                                            strings are opaque constants)
    BOOL     = 'true' | 'false'
    IDENT    = [A-Za-z_][A-Za-z0-9_]*
    keywords : func var if else while return true false
    operators: ( ) { } , ; = == != < <= > >= + - * / % ! && ||
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum, auto

from .errors import LexError
from .location import Span, make_span


class TokType(Enum):
    NUMBER = auto()
    STRING = auto()
    IDENT = auto()
    # keywords
    FUNC = auto()
    VAR = auto()
    IF = auto()
    ELSE = auto()
    WHILE = auto()
    RETURN = auto()
    TRUE = auto()
    FALSE = auto()
    # punctuation / operators
    LPAREN = auto()
    RPAREN = auto()
    LBRACE = auto()
    RBRACE = auto()
    COMMA = auto()
    SEMI = auto()
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
    BANG = auto()
    AND = auto()
    OR = auto()
    EOF = auto()


KEYWORDS = {
    "func": TokType.FUNC,
    "var": TokType.VAR,
    "if": TokType.IF,
    "else": TokType.ELSE,
    "while": TokType.WHILE,
    "return": TokType.RETURN,
    "true": TokType.TRUE,
    "false": TokType.FALSE,
}


@dataclass(frozen=True)
class Token:
    type: TokType
    value: str
    span: Span


_SIMPLE = {
    "(": TokType.LPAREN,
    ")": TokType.RPAREN,
    "{": TokType.LBRACE,
    "}": TokType.RBRACE,
    ",": TokType.COMMA,
    ";": TokType.SEMI,
    "+": TokType.PLUS,
    "-": TokType.MINUS,
    "*": TokType.STAR,
    "/": TokType.SLASH,
    "%": TokType.PERCENT,
}


def tokenize(source: str) -> list[Token]:
    tokens: list[Token] = []
    i, n = 0, len(source)

    while i < n:
        ch = source[i]

        # whitespace
        if ch in " \t\r\n":
            i += 1
            continue

        # // and /* */ comments
        if ch == "/" and i + 1 < n and source[i + 1] == "/":
            i += 2
            while i < n and source[i] != "\n":
                i += 1
            continue
        if ch == "/" and i + 1 < n and source[i + 1] == "*":
            start = i
            i += 2
            while i + 1 < n and not (source[i] == "*" and source[i + 1] == "/"):
                i += 1
            if i + 1 >= n:
                raise LexError("unterminated block comment", make_span(source, start, n))
            i += 2
            continue

        # numbers (integers only)
        if ch.isdigit():
            start = i
            while i < n and source[i].isdigit():
                i += 1
            tokens.append(Token(TokType.NUMBER, source[start:i], make_span(source, start, i)))
            continue

        # strings (escape: backslash + next char, kept verbatim except length)
        if ch == '"':
            start = i
            i += 1
            while i < n and source[i] != '"':
                if source[i] == "\\" and i + 1 < n:
                    i += 2
                    continue
                if source[i] == "\n":
                    raise LexError("unterminated string literal", make_span(source, start, i))
                i += 1
            if i >= n:
                raise LexError("unterminated string literal", make_span(source, start, n))
            i += 1  # closing quote
            tokens.append(Token(TokType.STRING, source[start:i], make_span(source, start, i)))
            continue

        # identifiers / keywords
        if ch.isalpha() or ch == "_":
            start = i
            while i < n and (source[i].isalnum() or source[i] == "_"):
                i += 1
            word = source[start:i]
            ttype = KEYWORDS.get(word, TokType.IDENT)
            tokens.append(Token(ttype, word, make_span(source, start, i)))
            continue

        # two-character operators
        two = source[i:i + 2]
        two_map = {"==": TokType.EQ, "!=": TokType.NE, "<=": TokType.LE,
                   ">=": TokType.GE, "&&": TokType.AND, "||": TokType.OR}
        if two in two_map:
            tokens.append(Token(two_map[two], two, make_span(source, i, i + 2)))
            i += 2
            continue

        if ch == "=":
            tokens.append(Token(TokType.ASSIGN, ch, make_span(source, i, i + 1)))
            i += 1
            continue
        if ch == "!":
            tokens.append(Token(TokType.BANG, ch, make_span(source, i, i + 1)))
            i += 1
            continue
        for sym, ttype in (("<", TokType.LT), (">", TokType.GT)):
            if ch == sym:
                tokens.append(Token(ttype, ch, make_span(source, i, i + 1)))
                i += 1
                break
        else:
            if ch in _SIMPLE:
                tokens.append(Token(_SIMPLE[ch], ch, make_span(source, i, i + 1)))
                i += 1
                continue
            raise LexError(f"unexpected character {ch!r}", make_span(source, i, i + 1))

    tokens.append(Token(TokType.EOF, "", make_span(source, n, n)))
    return tokens
