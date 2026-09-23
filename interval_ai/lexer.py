"""Hand-written lexer for IntervalLang.

No external/compiler libraries are used: every token is produced by scanning
the source character by character, and each token keeps its source location
(Span) so downstream diagnostics can point at the exact source text.
"""

from __future__ import annotations
from dataclasses import dataclass

from .errors import LexError, Span

# Token kinds:
#   INT          integer literal (mathematical precision, Python int)
#   IDENT        identifier
#   keyword      one of the KEYWORDS (kind == keyword text)
#   punctuation  one of PUNCT    (kind == punctuation text)
#   EOF

KEYWORDS = {
    "var", "array", "input", "if", "else", "while",
    "true", "false", "skip", "havoc",
}

# Multi-character punctuation must be tried before their single-character
# prefixes (e.g. "==" before "=").
PUNCT = [
    "==", "!=", "<=", ">=", "&&", "||",
    "+", "-", "*", "/", "%", "!",
    "<", ">", "=",
    "(", ")", "[", "]", "{", "}", ";", ",",
]

SINGLE = {c for p in PUNCT for c in p if len(c) == 1}


@dataclass(frozen=True)
class Token:
    kind: str
    text: str
    value: int | None
    span: Span


class Lexer:
    def __init__(self, source: str):
        self.src = source
        self.n = len(source)
        self.i = 0
        self.line = 1
        # col is 1-based and counts characters on the current line.
        self.col = 1

    def error(self, msg: str, start_off: int, line: int, col: int) -> LexError:
        return LexError(msg, Span(start_off, start_off + 1, line, col))

    def tokenize(self) -> list[Token]:
        out: list[Token] = []
        s = self.src
        while self.i < self.n:
            c = s[self.i]

            # Whitespace (not newlines; newline is handled uniformly below).
            if c in " \t\r":
                self.advance()
                continue
            if c == "\n":
                self.advance()
                continue

            # Line comments: // to end of line.
            if c == "/" and self.peek(1) == "/":
                while self.i < self.n and s[self.i] != "\n":
                    self.advance()
                continue
            # Block comments: /* ... */ (may nest).
            if c == "/" and self.peek(1) == "*":
                self.lex_block_comment()
                continue

            start, line, col = self.i, self.line, self.col

            if c.isdigit():
                text = self.lex_number()
                # Literals have mathematical-integer magnitude: no width cap.
                value = int(text)
                out.append(Token("INT", text, value,
                                 Span(start, self.i, line, col)))
                continue

            if c.isalpha() or c == "_":
                text = self.lex_ident()
                kind = text if text in KEYWORDS else "IDENT"
                out.append(Token(kind, text, None,
                                 Span(start, self.i, line, col)))
                continue

            matched = None
            for p in PUNCT:
                if s.startswith(p, self.i):
                    matched = p
                    break
            if matched is not None:
                for _ in matched:
                    self.advance()
                out.append(Token(matched, matched, None,
                                 Span(start, self.i, line, col)))
                continue

            raise self.error(f"unexpected character {c!r}", start, line, col)

        out.append(Token("EOF", "", None, Span(self.n, self.n, self.line, self.col)))
        return out

    def lex_block_comment(self) -> None:
        depth = 0
        while self.i < self.n:
            if self.src.startswith("/*", self.i):
                self.advance(); self.advance()
                depth += 1
            elif self.src.startswith("*/", self.i):
                self.advance(); self.advance()
                depth -= 1
                if depth == 0:
                    return
            else:
                self.advance()
        raise self.error("unterminated block comment", self.i,
                         self.line, self.col)

    def lex_number(self) -> str:
        start = self.i
        while self.i < self.n and self.src[self.i].isdigit():
            self.advance()
        return self.src[start:self.i]

    def lex_ident(self) -> str:
        start = self.i
        while self.i < self.n and (self.src[self.i].isalnum()
                                  or self.src[self.i] == "_"):
            self.advance()
        return self.src[start:self.i]

    def peek(self, k: int) -> str:
        j = self.i + k
        return self.src[j] if j < self.n else ""

    def advance(self) -> None:
        if self.src[self.i] == "\n":
            self.line += 1
            self.col = 1
        else:
            self.col += 1
        self.i += 1
