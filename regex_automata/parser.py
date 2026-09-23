"""Recursive-descent parser for the regex subset.

Grammar (see ``docs/language.md``)::

    alternation := concat ('|' concat)*
    concat      := quantified*
    quantified  := atom ('*' | '+' | '?' | '{'m'}' | '{'m','}' | '{'m','n'}')?
    atom        := CHAR | DOT | CLASS | ANCHOR | '(' alternation ')'

Empty alternatives and the fully empty pattern are allowed.  Lazy
(``*?``) and possessive (``*+``) quantifier markers are explicitly
rejected with a positioned error.
"""
from __future__ import annotations

from . import ast
from .errors import ParseError
from .locations import Span
from .tokens import (
    T_ANCHOR,
    T_CHAR,
    T_CLASS,
    T_DOT,
    T_EOF,
    T_LPAREN,
    T_PIPE,
    T_QUANT,
    T_RPAREN,
    Token,
)


class Parser:
    def __init__(self, source: str, tokens: list[Token]) -> None:
        self.source = source
        self.tokens = tokens
        self.pos = 0

    # helpers ------------------------------------------------------------------
    @property
    def tok(self) -> Token:
        return self.tokens[self.pos]

    def advance(self) -> Token:
        t = self.tokens[self.pos]
        if self.pos < len(self.tokens) - 1:
            self.pos += 1
        return t

    def _error(self, message: str, span: Span) -> ParseError:
        return ParseError(message, span, self.source)

    # grammar ------------------------------------------------------------------
    def parse(self) -> ast.Node:
        node = self.parse_alternation()
        if self.tok.kind != T_EOF:
            # Only an unmatched ')' can survive to here.
            raise self._error("unbalanced ')' with no matching '('", self.tok.span)
        return node

    def parse_alternation(self) -> ast.Node:
        start = self.tok.span.start
        branches = [self.parse_concat()]
        while self.tok.kind == T_PIPE:
            pipe = self.advance()
            branches.append(self.parse_concat())
        if len(branches) == 1:
            return branches[0]
        end = self.tok.span.start
        return ast.Alt(Span(start, end), tuple(branches))

    def parse_concat(self) -> ast.Node:
        start = self.tok.span.start
        parts: list[ast.Node] = []
        while self.tok.kind not in (T_PIPE, T_RPAREN, T_EOF):
            parts.append(self.parse_quantified())
        if not parts:
            return ast.Empty(Span(start, start))
        if len(parts) == 1:
            return parts[0]
        return ast.Concat(Span(start, parts[-1].span.end), tuple(parts))

    def parse_quantified(self) -> ast.Node:
        if self.tok.kind == T_QUANT:
            q = self.tok
            raise self._error("quantifier with nothing to repeat", q.span)
        child = self.parse_atom()
        while self.tok.kind == T_QUANT:
            q = self.advance()
            # Consecutive *bare* quantifiers (e.g. ``a**``) are illegal, but a
            # quantifier on a *group* containing one (``(a*)*``) is fine: the
            # atom there is a parenthesised node, not the Repeat itself.
            if isinstance(child, ast.Repeat):
                raise self._error(
                    "multiple quantifiers: a quantifier may not apply directly "
                    "to another quantifier (wrap it in parentheses if intended)",
                    q.span,
                )
            # Reject lazy/possessive markers that lexer kept as following chars.
            if q.span.end < len(self.source):
                marker = self.source[q.span.end]
                if marker == "?":
                    raise self._error(
                        "non-greedy quantifier marker '?' is not supported "
                        "(this engine supports greedy finite repetition only)",
                        Span(q.span.end, q.span.end + 1),
                    )
                if marker == "+":
                    raise self._error(
                        "possessive quantifier marker '+' is not supported",
                        Span(q.span.end, q.span.end + 1),
                    )
            child = ast.Repeat(
                Span(child.span.start, q.span.end),
                child,
                q.minimum,  # type: ignore[arg-type]
                q.maximum,
            )
        return child

    def parse_atom(self) -> ast.Node:
        t = self.tok
        if t.kind == T_CHAR:
            self.advance()
            return ast.Char(t.span, t.cp)  # type: ignore[arg-type]
        if t.kind == T_DOT:
            self.advance()
            return ast.Dot(t.span)
        if t.kind == T_CLASS:
            self.advance()
            return ast.Class(t.span, t.predicate)  # type: ignore[arg-type]
        if t.kind == T_ANCHOR:
            self.advance()
            return ast.Anchor(t.span, t.anchor)  # type: ignore[arg-type]
        if t.kind == T_LPAREN:
            return self.parse_group()
        # Defensive: stray quantifier/paren/EOF
        raise self._error(f"unexpected token {t.kind} while parsing atom", t.span)

    def parse_group(self) -> ast.Node:
        lp = self.advance()  # '('
        # Reject unsupported group modifiers explicitly (helpful errors).
        if lp.span.end < len(self.source) and self.source[lp.span.end] == "?":
            nxt = (
                self.source[lp.span.end + 1]
                if lp.span.end + 1 < len(self.source)
                else ""
            )
            if nxt != "":
                raise self._error(
                    f"group '(?{nxt}...)' is not supported: capturing groups, "
                    "lookarounds and mode flags are outside this subset",
                    Span(lp.span.start, lp.span.end + 2),
                )
        inner = self.parse_alternation()
        if self.tok.kind != T_RPAREN:
            raise self._error("unbalanced '(' with no matching ')'", lp.span)
        rp = self.advance()
        return ast.Group(lp.span.merge(rp.span), inner)
