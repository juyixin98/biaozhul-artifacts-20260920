"""Tokenizer and recursive-descent parser for the Datalog dialect.

Grammar (``?`` is only accepted by :func:`parse_query`, not in programs)::

    program  := (rule | fact)*
    rule     := atom ':-' literal (',' literal)* '.'
    fact     := atom '.'
    literal  := ['not'] atom
    atom     := IDENT ['(' term (',' term)* ')']
    term     := VAR | NUMBER | IDENT | STRING
    query    := atom '?'

- Variables start with an uppercase letter: ``X``, ``Person``.
- Constants are integers (``42``), lowercase symbols (``alice``) or quoted
  strings (``"Alice"``).
- ``not`` is a reserved keyword when it starts a literal.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional, Tuple

from .ast import Atom, Constant, Literal, Program, Rule, Term, Variable
from .errors import ParseError

_TOKEN_PUNCT = {"(", ")", ",", ".", "?", ":-"}


@dataclass(frozen=True)
class Token:
    kind: str  # VAR, IDENT, NOT, NUMBER, STRING, PUNCT, EOF
    value: object
    pos: int
    line: int
    col: int


class _Cursor:
    """Mutable position over the source text (index / line / column)."""

    def __init__(self, text: str):
        self.text = text
        self.n = len(text)
        self.i = 0
        self.line = 1
        self.col = 1

    @property
    def ch(self) -> str:
        return self.text[self.i]

    def advance(self, ch: Optional[str] = None) -> Tuple[int, int]:
        old_line, old_col = self.line, self.col
        ch = self.ch if ch is None else ch
        self.i += len(ch)
        if ch == "\n":
            self.line += 1
            self.col = 1
        else:
            self.col += len(ch)
        return old_line, old_col


def tokenize(text: str) -> List[Token]:
    cursor = _Cursor(text)
    tokens: List[Token] = []

    while cursor.i < cursor.n:
        ch = cursor.ch
        if ch in " \t\r\n":
            cursor.advance(ch)
            continue
        if ch == "%":  # line comment
            while cursor.i < cursor.n and cursor.ch != "\n":
                cursor.advance()
            continue
        start, sline, scol = cursor.i, cursor.line, cursor.col
        if ch == ":" and cursor.i + 1 < cursor.n and cursor.text[cursor.i + 1] == "-":
            cursor.advance(":")
            cursor.advance("-")
            tokens.append(Token("PUNCT", ":-", start, sline, scol))
        elif ch in "(),.?":
            cursor.advance(ch)
            tokens.append(Token("PUNCT", ch, start, sline, scol))
        elif ch == '"':
            tokens.append(_scan_string(cursor, start, sline, scol))
        elif ch.isdigit():
            tokens.append(_scan_number(cursor, start, sline, scol))
        elif ch.isalpha() or ch == "_":
            tokens.append(_scan_word(cursor, start, sline, scol))
        else:
            raise ParseError(
                f"unexpected character {ch!r} (line {cursor.line}, col {cursor.col})",
                details={"line": cursor.line, "col": cursor.col},
            )

    tokens.append(Token("EOF", None, cursor.i, cursor.line, cursor.col))
    return tokens


_ESCAPES = {"n": "\n", "t": "\t", '"': '"', "\\": "\\"}


def _scan_string(cursor: _Cursor, start: int, line: int, col: int) -> Token:
    cursor.advance('"')
    chars: List[str] = []
    closed = False
    while cursor.i < cursor.n:
        c = cursor.ch
        if c == '"':
            cursor.advance(c)
            closed = True
            break
        if c == "\n":
            raise ParseError(
                f"unterminated string literal (line {line}, col {col})",
                details={"line": line, "col": col},
            )
        if c == "\\":
            cursor.advance(c)
            if cursor.i >= cursor.n:
                break
            esc = cursor.ch
            chars.append(_ESCAPES.get(esc, esc))
            cursor.advance(esc)
        else:
            chars.append(c)
            cursor.advance(c)
    if not closed:
        raise ParseError(
            f"unterminated string literal (line {line}, col {col})",
            details={"line": line, "col": col},
        )
    return Token("STRING", "".join(chars), start, line, col)


def _scan_number(cursor: _Cursor, start: int, line: int, col: int) -> Token:
    digits = ""
    while cursor.i < cursor.n and cursor.ch.isdigit():
        digits += cursor.ch
        cursor.advance()
    if len(digits) > 1 and digits[0] == "0":
        raise ParseError(
            f"number with leading zero: {digits} (line {line}, col {col})",
            details={"line": line, "col": col},
        )
    return Token("NUMBER", int(digits), start, line, col)


def _scan_word(cursor: _Cursor, start: int, line: int, col: int) -> Token:
    first = cursor.ch
    word = ""
    while cursor.i < cursor.n and (cursor.ch.isalnum() or cursor.ch == "_"):
        word += cursor.ch
        cursor.advance()
    if word == "not":
        return Token("NOT", word, start, line, col)
    kind = "VAR" if first.isupper() else "IDENT"
    return Token(kind, word, start, line, col)


class _Parser:
    def __init__(self, text: str):
        self.text = text
        self.tokens = tokenize(text)
        self.idx = 0

    @property
    def cur(self) -> Token:
        return self.tokens[self.idx]

    def _loc(self, token: Optional[Token] = None) -> dict:
        t = token or self.cur
        return {"line": t.line, "col": t.col}

    def expect(self, punct: str) -> Token:
        t = self.cur
        if t.kind == "PUNCT" and t.value == punct:
            self.idx += 1
            return t
        raise ParseError(
            f"expected {punct!r} but found {self._describe(t)}",
            details={"line": t.line, "col": t.col},
        )

    @staticmethod
    def _describe(t: Token) -> str:
        if t.kind == "EOF":
            return "end of input"
        return repr(t.value)

    def parse_program(self) -> Program:
        facts: List[Atom] = []
        rules: List[Rule] = []
        while self.cur.kind != "EOF":
            atom = self.parse_atom()
            if self.cur.kind == "PUNCT" and self.cur.value == ".":
                self.expect(".")
                self._assert_ground_fact(atom)
                facts.append(atom)
            elif self.cur.kind == "PUNCT" and self.cur.value == ":-":
                self.expect(":-")
                body = [self.parse_literal()]
                while self.cur.kind == "PUNCT" and self.cur.value == ",":
                    self.expect(",")
                    body.append(self.parse_literal())
                self.expect(".")
                rules.append(Rule(head=atom, body=tuple(body)))
            else:
                t = self.cur
                raise ParseError(
                    f"expected '.' or ':-' after {atom.canonical()} but found {self._describe(t)}",
                    details={"line": t.line, "col": t.col},
                )
        return Program(facts=tuple(facts), rules=tuple(rules), source_text=self.text)

    def _assert_ground_fact(self, atom: Atom) -> None:
        for term in atom.args:
            if isinstance(term, Variable):
                line = self.cur.line
                raise ParseError(
                    f"fact {atom.canonical()} contains variable {term.name}; "
                    f"facts must be ground (line {line})",
                    details={"line": line, "predicate": atom.predicate, "variable": term.name},
                )

    def parse_literal(self) -> Literal:
        negated = False
        if self.cur.kind == "NOT":
            negated = True
            self.idx += 1
        atom = self.parse_atom()
        return Literal(atom=atom, negated=negated)

    def parse_atom(self) -> Atom:
        t = self.cur
        if t.kind != "IDENT":
            raise ParseError(
                f"predicate name must be a lowercase identifier but found {self._describe(t)}",
                details={"line": t.line, "col": t.col},
            )
        self.idx += 1
        args: List[Term] = []
        if self.cur.kind == "PUNCT" and self.cur.value == "(":
            self.expect("(")
            if not (self.cur.kind == "PUNCT" and self.cur.value == ")"):
                args.append(self.parse_term())
                while self.cur.kind == "PUNCT" and self.cur.value == ",":
                    self.expect(",")
                    args.append(self.parse_term())
            self.expect(")")
        return Atom(predicate=str(t.value), args=tuple(args))

    def parse_term(self) -> Term:
        t = self.cur
        if t.kind == "VAR":
            self.idx += 1
            return Variable(str(t.value))
        if t.kind == "NUMBER":
            self.idx += 1
            return Constant(int(t.value))  # type: ignore[arg-type]
        if t.kind == "STRING":
            self.idx += 1
            return Constant(str(t.value))
        if t.kind == "IDENT":
            self.idx += 1
            return Constant(str(t.value))
        raise ParseError(
            f"expected a term but found {self._describe(t)}",
            details={"line": t.line, "col": t.col},
        )

    def parse_query_goal(self) -> Atom:
        atom = self.parse_atom()
        self.expect("?")
        if self.cur.kind != "EOF":
            t = self.cur
            raise ParseError(
                f"trailing input after query: {self._describe(t)}",
                details={"line": t.line, "col": t.col},
            )
        return atom


def parse_program(text: str) -> Program:
    return _Parser(text).parse_program()


def parse_query(text: str) -> Atom:
    return _Parser(text).parse_query_goal()
