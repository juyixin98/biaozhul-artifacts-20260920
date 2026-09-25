"""Hand-written lexer for the Mini language.

No part of this module (or the package) uses an external parser/compiler
generator: tokens are scanned character by character.

Token kinds:

``LET`` ``FN`` ``IDENT`` ``NUMBER`` ``STRING`` ``PUNCT`` ``EOF``.

Every token carries absolute character offsets.  Lexer errors (illegal
characters, unterminated strings) are collected as :class:`Diagnostic`
objects rather than raised, so a document always yields a token stream.
"""
from __future__ import annotations

from bisect import bisect_right
from dataclasses import dataclass
from typing import List, Tuple


KEYWORDS = {
    "let": "LET",
    "fn": "FN",
}

PUNCTUATORS = set("=;(),+-*/%")
SINGLE_SPACE_TOKENS = PUNCTUATORS  # every punct is a single character


@dataclass
class Token:
    kind: str
    start: int
    end: int
    text: str
    ok: bool = True  # False for unterminated strings

    def __repr__(self) -> str:  # pragma: no cover
        return f"Token({self.kind}, {self.start}..{self.end}, {self.text!r})"


@dataclass
class Diagnostic:
    message: str
    start: int
    end: int

    def signature(self) -> Tuple[str, int, int]:
        return self.message, self.start, self.end

    def to_dict(self, line_starts: List[int] | None = None) -> dict:
        data = {
            "message": self.message,
            "start": self.start,
            "end": self.end,
        }
        if line_starts is not None:
            sl, sc = offset_to_line_col(line_starts, self.start)
            el, ec = offset_to_line_col(line_starts, self.end)
            data.update(
                start_line=sl,
                start_col=sc,
                end_line=el,
                end_col=ec,
            )
        return data


def compute_line_starts(text: str) -> List[int]:
    """Offset of the first character of every line (line 0 starts at 0)."""
    starts = [0]
    for i, ch in enumerate(text):
        if ch == "\n":
            starts.append(i + 1)
    return starts


def offset_to_line_col(line_starts: List[int], offset: int) -> Tuple[int, int]:
    """0-based (line, column) for a character offset."""
    line = bisect_right(line_starts, offset) - 1
    return line, offset - line_starts[line]


def is_ident_start(ch: str) -> bool:
    return ch == "_" or ch.isalpha()


def is_ident_part(ch: str) -> bool:
    return ch == "_" or ch.isalnum()


def lex(text: str) -> Tuple[List[Token], List[Diagnostic]]:
    """Lex *text* into ``(tokens, diagnostics)``.

    The token list always ends with an ``EOF`` token at ``len(text)``.
    """
    tokens: List[Token] = []
    diagnostics: List[Diagnostic] = []
    n = len(text)
    i = 0

    while i < n:
        ch = text[i]

        if ch in " \t\r\n":
            i += 1
            continue

        # Line comment: // runs to end of line.  Inside strings the lexer
        # never enters this branch, so "a // b" is ordinary string content.
        if ch == "/" and i + 1 < n and text[i + 1] == "/":
            i += 2
            while i < n and text[i] != "\n":
                i += 1
            continue

        # Identifiers and keywords.
        if is_ident_start(ch):
            start = i
            i += 1
            while i < n and is_ident_part(text[i]):
                i += 1
            word = text[start:i]
            kind = KEYWORDS.get(word, "IDENT")
            tokens.append(Token(kind, start, i, word))
            continue

        # Integer or decimal number: digits [. digits]
        if ch.isdigit():
            start = i
            i += 1
            while i < n and text[i].isdigit():
                i += 1
            if i + 1 < n and text[i] == "." and text[i + 1].isdigit():
                i += 1
                while i < n and text[i].isdigit():
                    i += 1
            tokens.append(Token("NUMBER", start, i, text[start:i]))
            continue

        # String literal with backslash escapes.  A string terminates at the
        # closing quote; hitting a newline or EOF first is a lexer error.
        if ch == '"':
            start = i
            i += 1
            closed = False
            while i < n:
                c = text[i]
                if c == "\\" and i + 1 < n:
                    i += 2  # escape the next character, even a newline
                    continue
                if c == '"':
                    i += 1
                    closed = True
                    break
                if c == "\n":
                    break
                i += 1
            raw = text[start:i]
            if not closed:
                diagnostics.append(
                    Diagnostic("unterminated string literal", start, i)
                )
            tokens.append(Token("STRING", start, i, raw, ok=closed))
            continue

        if ch in PUNCTUATORS:
            tokens.append(Token("PUNCT", i, i + 1, ch))
            i += 1
            continue

        # Illegal character: report it and skip exactly one character,
        # giving a precise diagnostic position and deterministic recovery.
        diagnostics.append(
            Diagnostic(f"illegal character {ch!r}", i, i + 1)
        )
        i += 1

    tokens.append(Token("EOF", n, n, ""))
    return tokens, diagnostics


def decode_string_literal(raw: str) -> str:
    """Decode the text of a STRING token (without surrounding quotes)."""
    if len(raw) >= 2 and raw.endswith('"'):
        end = len(raw) - 1  # drop closing quote
    else:
        end = len(raw)  # unterminated
    escapes = {"n": "\n", "t": "\t", "r": "\r", '"': '"', "\\": "\\"}
    out = []
    i = 1  # skip opening quote
    while i < end:
        ch = raw[i]
        if ch == "\\" and i + 1 < end:
            out.append(escapes.get(raw[i + 1], raw[i + 1]))
            i += 2
        else:
            out.append(ch)
            i += 1
    return "".join(out)
