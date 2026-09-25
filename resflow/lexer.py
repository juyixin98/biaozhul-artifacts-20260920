"""Hand-written lexer for the ResFlow language.

The lexer is deliberately dependency free: it tracks byte offsets and maps
them to 1-based line/column pairs so every token (and therefore every AST
node and diagnostic) can be traced back to the exact source span.
"""

from dataclasses import dataclass

from .errors import ResFlowError

# Reserved words.  ``use`` is reserved because USE-FREE hinges on it.
KEYWORDS = {
    "fn", "let", "acquire", "release", "use", "return",
    "throw", "if", "else", "while", "try", "catch",
}
# Literal keywords get their own token kind in _lex_word.
LITERAL_KEYWORDS = {"true", "false"}


@dataclass(frozen=True)
class Location:
    """A half-open source span ``[offset, end_offset)`` with line/column."""

    line: int
    column: int
    offset: int
    end_offset: int

    def to_dict(self):
        return {
            "line": self.line,
            "column": self.column,
            "offset": self.offset,
            "end_offset": self.end_offset,
        }


class Token:
    __slots__ = ("kind", "value", "loc")

    def __init__(self, kind, value, loc):
        self.kind = kind
        self.value = value
        self.loc = loc

    def __repr__(self):
        return f"Token({self.kind!r}, {self.value!r}, {self.loc})"


class LexError(ResFlowError):
    pass


def _is_ident_start(ch):
    return ch == "_" or ch.isalpha()


def _is_ident_part(ch):
    return ch == "_" or ch.isalnum()


class Lexer:
    def __init__(self, source):
        self.source = source
        self.n = len(source)
        self.pos = 0
        self.line = 1
        self.line_start = 0  # offset where the current line begins

    # -- location helpers --------------------------------------------------

    def _loc(self, start, end=None):
        if end is None:
            end = start + 1
        col = start - self.line_start + 1
        return Location(self.line, col, start, end)

    def _advance(self):
        ch = self.source[self.pos]
        self.pos += 1
        if ch == "\n":
            self.line += 1
            self.line_start = self.pos
        return ch

    # -- public entry point ------------------------------------------------

    def tokenize(self):
        tokens = []
        src = self.source
        while self.pos < self.n:
            ch = src[self.pos]
            if ch in " \t\r\n":
                self._advance()
                continue
            if ch == "/" and self.pos + 1 < self.n and src[self.pos + 1] == "/":
                while self.pos < self.n and src[self.pos] != "\n":
                    self.pos += 1
                continue
            if ch == "/" and self.pos + 1 < self.n and src[self.pos + 1] == "*":
                self._lex_block_comment()
                continue
            start = self.pos
            if _is_ident_start(ch):
                tokens.append(self._lex_word(start))
                continue
            if ch.isdigit():
                tokens.append(self._lex_number(start))
                continue
            if ch == '"':
                tokens.append(self._lex_string(start))
                continue
            tokens.append(self._lex_punct(start))
        tokens.append(Token("EOF", None, self._loc(self.pos, self.pos)))
        return tokens

    # -- individual token kinds -------------------------------------------

    def _lex_block_comment(self):
        start = self.pos
        self._advance()  # /
        self._advance()  # *
        depth = 1
        while self.pos < self.n and depth:
            ch = self.source[self.pos]
            if ch == "/" and self.pos + 1 < self.n and self.source[self.pos + 1] == "*":
                self._advance()
                self._advance()
                depth += 1
            elif ch == "*" and self.pos + 1 < self.n and self.source[self.pos + 1] == "/":
                self._advance()
                self._advance()
                depth -= 1
            else:
                self._advance()
        if depth:
            raise LexError("unterminated block comment", self._loc(start, self.pos))

    def _lex_word(self, start):
        self._advance()
        while self.pos < self.n and _is_ident_part(self.source[self.pos]):
            self._advance()
        text = self.source[start:self.pos]
        if text in LITERAL_KEYWORDS:
            return Token("BOOL", text, self._loc(start, self.pos))
        if text in KEYWORDS:
            return Token("KEYWORD", text, self._loc(start, self.pos))
        return Token("IDENT", text, self._loc(start, self.pos))

    def _lex_number(self, start):
        self._advance()
        while self.pos < self.n and self.source[self.pos].isdigit():
            self._advance()
        text = self.source[start:self.pos]
        return Token("INT", int(text), self._loc(start, self.pos))

    def _lex_string(self, start):
        self._advance()  # opening quote
        buf = []
        while self.pos < self.n and self.source[self.pos] != '"':
            ch = self.source[self.pos]
            if ch == "\\":
                self._advance()
                if self.pos >= self.n:
                    break
                esc = self.source[self.pos]
                buf.append({"n": "\n", "t": "\t", '"': '"',
                            "\\": "\\", "0": "\0"}.get(esc, esc))
                self._advance()
            elif ch == "\n":
                raise LexError("unterminated string literal",
                               self._loc(start, self.pos))
            else:
                buf.append(ch)
                self._advance()
        if self.pos >= self.n:
            raise LexError("unterminated string literal",
                           self._loc(start, self.pos))
        self._advance()  # closing quote
        return Token("STRING", "".join(buf), self._loc(start, self.pos))

    # Two-character operators are matched greedily before single characters.
    _PUNCT2 = {"==", "!=", "<=", ">=", "&&", "||"}
    _PUNCT1 = set("(){};,=!<>&|+-*/%")

    def _lex_punct(self, start):
        two = self.source[self.pos:self.pos + 2]
        if two in self._PUNCT2:
            self._advance()
            self._advance()
            # Multi-character operators carry their spelling as the kind.
            return Token(two, two, self._loc(start, self.pos))
        ch = self.source[self.pos]
        if ch in self._PUNCT1:
            self._advance()
            # Single-character punctuation uses the canonical kind names
            # the parser expects; the spelling is kept in ``value``.
            kind = {"(": "LPAREN", ")": "RPAREN", "{": "LBRACE",
                    "}": "RBRACE", ";": "SEMI", ",": "COMMA"}.get(ch, ch)
            return Token(kind, ch, self._loc(start, self.pos))
        raise LexError(f"unexpected character {ch!r}", self._loc(start))
