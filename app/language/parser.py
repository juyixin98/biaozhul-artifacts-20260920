"""Recursive-descent parser producing the AST in :mod:`app.language.terms`.

Naming convention for identifiers
---------------------------------
* starts with an uppercase letter  -> variable (``X``, ``Who``)
* equals ``_``                      -> wildcard (fresh anonymous variable)
* anything else                     -> constant (``ann``, ``12``)

A fact is a clause with no body and must be ground; a rule head may only
contain variables that occur in a positive body literal (checked by the
compiler, not the parser).
"""

from __future__ import annotations

from .lexer import (
    T_COMMA,
    T_CMP,
    T_DOT,
    T_EOF,
    T_IDENT,
    T_LPAREN,
    T_NOT,
    T_RPAREN,
    T_STRING,
    T_TURNSTILE,
    Token,
    Tokenizer,
)
from .terms import Atom, Comparison, Const, Literal, NegAtom, Program, Rule, Term, Var


class ParseError(ValueError):
    def __init__(self, message: str, token: Token | None = None) -> None:
        if token is not None:
            message = f"parse error at offset {token.pos} (near {token.value!r}): {message}"
        super().__init__(message)
        self.token = token


class Parser:
    def __init__(self, source: str) -> None:
        self.tokens = Tokenizer(source).tokenize()
        self.pos = 0
        self._wildcard_counter = 0

    # -- token helpers ------------------------------------------------------

    @property
    def cur(self) -> Token:
        return self.tokens[self.pos]

    def _advance(self) -> Token:
        tok = self.tokens[self.pos]
        if self.pos < len(self.tokens) - 1:
            self.pos += 1
        return tok

    def _expect(self, kind: str) -> Token:
        if self.cur.kind != kind:
            raise ParseError(f"expected {kind}, got {self.cur.kind}", self.cur)
        return self._advance()

    # -- grammar ------------------------------------------------------------

    def parse_program(self) -> Program:
        rules: list[Rule] = []
        facts: list[Atom] = []
        while self.cur.kind != T_EOF:
            head = self._parse_atom()
            if self.cur.kind == T_DOT:
                self._advance()
                self._check_ground_fact(head)
                facts.append(head)
            elif self.cur.kind == T_TURNSTILE:
                self._advance()
                body = self._parse_body()
                self._expect(T_DOT)
                rules.append(Rule(head=head, body=tuple(body)))
            else:
                raise ParseError("expected '.' or ':-' after clause head", self.cur)
        return Program(rules=tuple(rules), facts=tuple(facts))

    def _parse_body(self) -> list[Literal]:
        literals: list[Literal] = [self._parse_literal()]
        while self.cur.kind == T_COMMA:
            self._advance()
            literals.append(self._parse_literal())
        return literals

    def _parse_literal(self) -> Literal:
        negated = False
        if self.cur.kind == T_NOT:
            self._advance()
            negated = True
        first = self._parse_term()
        if self.cur.kind == T_CMP:
            if negated:
                raise ParseError("NOT is only valid before relational atoms", self.cur)
            op = self._advance().value
            second = self._parse_term()
            return Comparison(op=op, left=first, right=second)
        atom = self._finish_atom(first)
        return NegAtom(atom) if negated else atom

    def _parse_atom(self) -> Atom:
        first = self._parse_term()
        return self._finish_atom(first)

    def _finish_atom(self, first: Term) -> Atom:
        if not isinstance(first, Const):
            raise ParseError("predicate name must be a constant", self.cur)
        self._expect(T_LPAREN)
        args: list[Term] = []
        if self.cur.kind != T_RPAREN:
            args.append(self._parse_term())
            while self.cur.kind == T_COMMA:
                self._advance()
                args.append(self._parse_term())
        self._expect(T_RPAREN)
        return Atom(pred=first.value, args=tuple(args))

    def _parse_term(self) -> Term:
        tok = self.cur
        if tok.kind == T_IDENT:
            self._advance()
            if tok.value == "_":
                self._wildcard_counter += 1
                return Var(f"_w{self._wildcard_counter}")
            if tok.value[0].isupper():
                return Var(tok.value)
            return Const(tok.value)
        if tok.kind == T_STRING:
            self._advance()
            return Const(tok.value)
        raise ParseError(f"expected a term, got {tok.kind}", tok)

    # -- semantic checks ----------------------------------------------------

    @staticmethod
    def _check_ground_fact(atom: Atom) -> None:
        bad = sorted(v for v in atom.variables())
        if bad:
            raise ParseError(f"fact {atom.render()} contains variables {bad}")


def parse_program(source: str) -> Program:
    return Parser(source).parse_program()
