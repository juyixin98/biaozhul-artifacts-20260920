"""Hand-written lexer for the Imp language (see README / docs/syntax.md)."""

from .errors import LexError
from .source import Loc, Token

KEYWORDS = {
    "var", "arr", "input", "if", "else", "while",
    "true", "false", "and", "or", "not",
}

# Multi-character operators must be tried before their prefixes.
OPERATORS = ("==", "!=", "<=", ">=", "&&", "||",
             "=", "<", ">", "+", "-", "*", "/", "%", "!",
             "(", ")", "{", "}", "[", "]", ";", ",")

# && and || are the canonical spellings; the grammar also documents them.
SINGLE_CHAR = set("=<>+-*/%!(){}[];,")


def lex(src: str):
    tokens = []
    i, n = 0, len(src)
    line, col = 1, 1

    def make_loc(start, end):
        return Loc(line_at[start], col_at[start], start, end)

    # Precompute line/col so every token loc is cheap and exact.
    line_at = [0] * (n + 1)
    col_at = [0] * (n + 1)
    ln, co = 1, 1
    for k in range(n):
        line_at[k] = ln
        col_at[k] = co
        if src[k] == "\n":
            ln += 1
            co = 1
        else:
            co += 1
    line_at[n] = ln
    col_at[n] = co

    while i < n:
        c = src[i]

        # Whitespace
        if c in " \t\r\n":
            i += 1
            continue

        # Comments: // line comment and /* block comment */
        if c == "/" and i + 1 < n and src[i + 1] == "/":
            i += 2
            while i < n and src[i] != "\n":
                i += 1
            continue
        if c == "/" and i + 1 < n and src[i + 1] == "*":
            start = i
            i += 2
            while i < n and not (src[i] == "*" and i + 1 < n and src[i + 1] == "/"):
                i += 1
            if i >= n:
                raise LexError("unterminated block comment", Loc(line_at[start], col_at[start], start, start + 2))
            i += 2
            continue

        start = i

        # Identifiers / keywords
        if c.isalpha() or c == "_":
            i += 1
            while i < n and (src[i].isalnum() or src[i] == "_"):
                i += 1
            text = src[start:i]
            kind = text if text in KEYWORDS else "ID"
            tokens.append(Token(kind, text, make_loc(start, i)))
            continue

        # Integer literals (arbitrary precision; arithmetic is over Z)
        if c.isdigit():
            i += 1
            while i < n and src[i].isdigit():
                i += 1
            text = src[start:i]
            tokens.append(Token("INT", text, make_loc(start, i)))
            continue

        # Operators / punctuation
        matched = None
        for op in OPERATORS:
            if src.startswith(op, i):
                matched = op
                break
        if matched is None:
            raise LexError(f"unexpected character {c!r}", make_loc(i, i + 1))
        i += len(matched)
        tokens.append(Token(matched, matched, make_loc(start, i)))

    tokens.append(Token("EOF", "", Loc(line_at[n], col_at[n], n, n)))
    return tokens
