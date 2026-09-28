"""Tokenizer for the Datalog dialect.

Grammar (informal)::

    program  := ( rule | fact )*
    rule     := atom ":-" literal ("," literal)* "."
    fact     := atom "."
    literal  := ["NOT"] atom | term cmpop term
    cmpop    := "=" | "!=" | "<=" | ">=" | "<" | ">"

Tokens are produced by :class:`Tokenizer` as ``(kind, value, pos)`` triples.
``pos`` points at the start offset inside the source, used in diagnostics.
"""

from __future__ import annotations

from dataclasses import dataclass

# Token kinds
T_LPAREN = "LPAREN"
T_RPAREN = "RPAREN"
T_COMMA = "COMMA"
T_DOT = "DOT"
T_TURNSTILE = "TURNSTILE"
T_NOT = "NOT"
T_CMP = "CMP"
T_IDENT = "IDENT"      # bare word / number: ann, 12, foo_bar
T_STRING = "STRING"    # quoted literal "..."
T_EOF = "EOF"


@dataclass(frozen=True)
class Token:
    kind: str
    value: str
    pos: int


class LexError(ValueError):
    def __init__(self, message: str, pos: int) -> None:
        super().__init__(f"lex error at offset {pos}: {message}")
        self.pos = pos


_CMP_OPS = ("!=", "<=", ">=", "=", "<", ">")


class Tokenizer:
    def __init__(self, source: str) -> None:
        self.src = source
        self.n = len(source)
        self.i = 0

    # -- public -------------------------------------------------------------

    def tokenize(self) -> list[Token]:
        out: list[Token] = []
        while True:
            self._skip_ws_and_comments()
            if self.i >= self.n:
                out.append(Token(T_EOF, "", self.i))
                return out
            start = self.i
            ch = self.src[self.i]

            if ch == "(":
                self.i += 1
                out.append(Token(T_LPAREN, "(", start))
            elif ch == ")":
                self.i += 1
                out.append(Token(T_RPAREN, ")", start))
            elif ch == ",":
                self.i += 1
                out.append(Token(T_COMMA, ",", start))
            elif ch == ".":
                self.i += 1
                out.append(Token(T_DOT, ".", start))
            elif ch == '"':
                out.append(self._read_string())
            elif ch == "?":
                # Whole-line? comments use '%'; '?' starts a var too, handled
                # in identifiers. Keep it explicit: not used.
                raise LexError(f"unexpected character {ch!r}", start)
            elif ch == "-" and self._peek(1) == ":":
                out.append(Token(T_TURNSTILE, ":-", start))
                self.i += 2
            elif ch == ":" and self._peek(1) == "-":
                out.append(Token(T_TURNSTILE, ":-", start))
                self.i += 2
            elif self._starts_cmp():
                op = self._read_cmp()
                out.append(Token(T_CMP, op, start))
            else:
                out.append(self._read_word())

    # -- internals ----------------------------------------------------------

    def _skip_ws_and_comments(self) -> None:
        while self.i < self.n:
            ch = self.src[self.i]
            if ch in " \t\r\n":
                self.i += 1
            elif ch == "%":
                while self.i < self.n and self.src[self.i] != "\n":
                    self.i += 1
            else:
                return

    def _peek(self, ahead: int) -> str:
        j = self.i + ahead
        return self.src[j] if j < self.n else ""

    def _starts_cmp(self) -> bool:
        s = self.src[self.i : self.i + 2]
        return s in ("!=", "<=", ">=") or self.src[self.i] in "=<>"

    def _read_cmp(self) -> str:
        for op in _CMP_OPS:
            if self.src.startswith(op, self.i):
                self.i += len(op)
                return op
        raise LexError("invalid comparison operator", self.i)

    def _read_string(self) -> Token:
        start = self.i
        self.i += 1  # opening quote
        chars: list[str] = []
        while self.i < self.n:
            ch = self.src[self.i]
            if ch == '"':
                self.i += 1
                return Token(T_STRING, "".join(chars), start)
            if ch == "\\":
                self.i += 1
                if self.i >= self.n:
                    break
                esc = self.src[self.i]
                chars.append({"n": "\n", "t": "\t", '"': '"', "\\": "\\"}.get(esc, esc))
                self.i += 1
            else:
                chars.append(ch)
                self.i += 1
        raise LexError("unterminated string literal", start)

    def _read_word(self) -> Token:
        start = self.i
        ch = self.src[self.i]
        if not (ch.isalnum() or ch in "_?"):
            raise LexError(f"unexpected character {ch!r}", start)
        while self.i < self.n:
            c = self.src[self.i]
            if c.isalnum() or c in "_?-":
                # '-' allowed inside words but the ':-' token is matched
                # first; a bare '-' between words is lexed as part of the
                # word which is acceptable for identifiers like 'a-b'.
                self.i += 1
            else:
                break
        word = self.src[start : self.i]
        if word.upper() == "NOT":
            return Token(T_NOT, "NOT", start)
        return Token(T_IDENT, word, start)
