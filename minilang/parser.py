"""Recursive-descent parser for the Mini language.

Grammar (see README for the prose version)::

    program      := decl*
    decl         := "let" IDENT "=" expr ";"
                  | "fn"  IDENT "(" params? ")" "=" expr ";"
                  | error
    params       := IDENT ("," IDENT)*
    expr         := unary (("+"|"-"|"*"|"/"|"%") unary)*
    unary        := "-" unary | call
    call         := primary ("(" args? ")")*
    args         := expr ("," expr)*
    primary      := NUMBER | STRING | IDENT | "(" expr ")"

The parser is written from scratch.  Syntax errors do not raise; they are
collected as diagnostics and the parser recovers (missing punctuation is
synthesised, junk tokens are skipped to the next declaration keyword) so
that invalid source still produces a full tree with positions.

The same parser supports incremental parsing: :meth:`Parser.parse_program`
can be handed reuse candidates keyed by token start offset.  When the
tokens of an old declaration are identical (kind, lexeme *and* relative
offset) to the upcoming tokens, the old subtree is reused verbatim instead
of being reparsed.
"""
from __future__ import annotations

from typing import Dict, List, Optional, Tuple

from .lexer import Diagnostic, Token, decode_string_literal
from .nodes import Node, shift_node

# binary operator precedence (higher binds tighter)
BIN_PRECEDENCE = {"+": 1, "-": 1, "*": 2, "/": 2, "%": 2}

# declaration -> list of (node, first token index, one-past-last token index)
DeclRange = Tuple[Node, int, int]


class Parser:
    def __init__(self, tokens: List[Token], text_len: int) -> None:
        self.tokens = tokens
        self.text_len = text_len
        self.pos = 0
        self.diagnostics: List[Diagnostic] = []
        self.decl_ranges: List[DeclRange] = []
        # parser diagnostics emitted for each declaration, parallel to
        # decl_ranges; needed so reused declarations keep their diagnostics
        self.decl_diagnostics: List[List[Diagnostic]] = []
        self.reused = 0
        self.parsed = 0

    # ---- token cursor helpers ------------------------------------------------

    def cur(self) -> Token:
        return self.tokens[self.pos]

    def advance(self) -> Token:
        token = self.tokens[self.pos]
        if token.kind != "EOF":
            self.pos += 1
        return token

    def at_punct(self, ch: str) -> bool:
        t = self.cur()
        return t.kind == "PUNCT" and t.text == ch

    def expect_punct(self, ch: str) -> Optional[Token]:
        if self.at_punct(ch):
            return self.advance()
        t = self.cur()
        self.diagnostics.append(
            Diagnostic(f"expected '{ch}'", t.start, t.start)
        )
        return None

    def expect_ident(self) -> Optional[Token]:
        t = self.cur()
        if t.kind == "IDENT":
            return self.advance()
        self.diagnostics.append(
            Diagnostic("expected identifier", t.start, t.start)
        )
        return None

    def span(self, token: Token) -> dict:
        return {"start": token.start, "end": token.end}

    # ---- program level -------------------------------------------------------

    def parse_program(
        self,
        candidates: Optional[Dict[int, list]] = None,
    ) -> Node:
        decls: List[Node] = []
        while self.cur().kind != "EOF":
            token_start = self.pos
            diag_start = len(self.diagnostics)
            node: Optional[Node] = None
            if candidates is not None:
                node = self._try_reuse(candidates.get(self.cur().start))
            if node is None:
                node = self.parse_decl()
                self.parsed += 1
            self.decl_ranges.append((node, token_start, self.pos))
            self.decl_diagnostics.append(
                list(self.diagnostics[diag_start:])
            )
            decls.append(node)
        return Node("program", 0, self.text_len, {}, decls)

    def _try_reuse(self, candidates: Optional[list]) -> Optional[Node]:
        """Return an old node if its token slice matches the upcoming tokens.

        Match requires identical kinds, lexemes and *relative* offsets, so a
        whitespace-only edit (same tokens, moved gaps) still forces a reparse
        while node spans stay exact.

        A declaration's parse can also depend on the single token *after*
        its own slice: error recovery stops at the next ``let``/``fn``/EOF,
        and a missing-``;`` diagnostic is positioned at that lookahead
        token.  The lookahead's kind/lexeme must therefore match too, and
        when the declaration carries a diagnostic pointing at the lookahead
        position, the lookahead's relative offset must match as well.
        """
        if not candidates:
            return None
        for (
            node,
            old_tokens,
            tok0,
            tok1,
            delta,
            old_diags,
            lookahead_sensitive,
        ) in candidates:
            count = tok1 - tok0
            if self.pos + count > len(self.tokens) or count <= 0:
                continue
            old_base = old_tokens[tok0].start
            new_base = self.tokens[self.pos].start
            ok = True
            for k in range(count):
                old_t = old_tokens[tok0 + k]
                new_t = self.tokens[self.pos + k]
                if (
                    old_t.kind != new_t.kind
                    or old_t.text != new_t.text
                    or old_t.ok != new_t.ok
                    or (old_t.start - old_base) != (new_t.start - new_base)
                ):
                    ok = False
                    break
            if ok:
                # one token of lookahead (declarations never consume EOF,
                # so both token lists have a token at these indices)
                old_la = old_tokens[tok1]
                new_la = self.tokens[self.pos + count]
                if (
                    old_la.kind != new_la.kind
                    or old_la.text != new_la.text
                    or old_la.ok != new_la.ok
                ):
                    ok = False
                elif lookahead_sensitive and (
                    (old_la.start - old_base) != (new_la.start - new_base)
                ):
                    ok = False
            if not ok:
                continue
            shift_node(node, delta)
            # The reused declaration keeps any diagnostics it was parsed
            # with, translated to the new offsets.
            for diag in old_diags:
                self.diagnostics.append(
                    Diagnostic(
                        diag.message, diag.start + delta, diag.end + delta
                    )
                )
            self.pos += count
            self.reused += 1
            return node
        return None

    # ---- declarations --------------------------------------------------------

    def parse_decl(self) -> Node:
        t = self.cur()
        if t.kind == "LET":
            return self.parse_let()
        if t.kind == "FN":
            return self.parse_fn()

        # Recovery: no declaration keyword.  Consume junk until the next
        # keyword/EOF and emit one error node covering the skipped text.
        start = t.start
        pieces: List[str] = []
        while self.cur().kind not in ("LET", "FN", "EOF"):
            pieces.append(self.cur().text)
            self.advance()
        end = self.tokens[self.pos - 1].end if self.pos > 0 else t.start
        self.diagnostics.append(
            Diagnostic(
                f"unexpected token {t.text!r}: expected a declaration",
                start,
                end,
            )
        )
        return Node("error", start, end, {"skipped": " ".join(pieces)})

    def parse_let(self) -> Node:
        let_tok = self.advance()  # 'let'
        name_tok = self.expect_ident()
        self.expect_punct("=")
        value = self.parse_expr()
        semi = self.expect_punct(";")
        end = semi.end if semi is not None else value.end
        node_value = {
            "name": name_tok.text if name_tok else None,
            "name_span": self.span(name_tok) if name_tok else None,
        }
        return Node("let", let_tok.start, end, node_value, [value])

    def parse_fn(self) -> Node:
        fn_tok = self.advance()  # 'fn'
        name_tok = self.expect_ident()
        params: List[dict] = []
        closed_paren = True

        open_tok = self.cur()
        if not self.at_punct("("):
            self.diagnostics.append(
                Diagnostic("expected '('", open_tok.start, open_tok.start)
            )
            closed_paren = False
            open_tok = None
        else:
            open_tok = self.advance()
            if self.cur().kind == "IDENT":
                params.append(self._parse_param())
                while self.at_punct(","):
                    self.advance()
                    param_tok = self.expect_ident()
                    if param_tok is not None:
                        params.append(self._param_from(param_tok))
            if self.at_punct(")"):
                self.advance()
            else:
                self.diagnostics.append(
                    Diagnostic(
                        "unclosed '('", open_tok.start, open_tok.end
                    )
                )
                closed_paren = False

        self.expect_punct("=")
        body = self.parse_expr()
        semi = self.expect_punct(";")
        end = semi.end if semi is not None else body.end
        node_value = {
            "name": name_tok.text if name_tok else None,
            "name_span": self.span(name_tok) if name_tok else None,
            "params": params,
            "closed_paren": closed_paren,
        }
        return Node("fn", fn_tok.start, end, node_value, [body])

    def _parse_param(self) -> dict:
        token = self.advance()  # IDENT
        return self._param_from(token)

    @staticmethod
    def _param_from(token: Token) -> dict:
        return {"name": token.text, "span": {"start": token.start, "end": token.end}}

    # ---- expressions ---------------------------------------------------------

    def parse_expr(self, min_precedence: int = 1) -> Node:
        left = self.parse_unary()
        while (
            self.cur().kind == "PUNCT"
            and self.cur().text in BIN_PRECEDENCE
            and BIN_PRECEDENCE[self.cur().text] >= min_precedence
        ):
            op_tok = self.advance()
            right = self.parse_expr(BIN_PRECEDENCE[op_tok.text] + 1)
            left = Node(
                "binary",
                left.start,
                right.end,
                {"op": op_tok.text, "op_span": self.span(op_tok)},
                [left, right],
            )
        return left

    def parse_unary(self) -> Node:
        if self.at_punct("-"):
            op_tok = self.advance()
            operand = self.parse_unary()
            return Node(
                "unary",
                op_tok.start,
                operand.end,
                {"op": "-", "op_span": self.span(op_tok)},
                [operand],
            )
        return self.parse_call()

    def parse_call(self) -> Node:
        expr = self.parse_primary()
        while self.at_punct("("):
            open_tok = self.advance()
            args: List[Node] = []
            if not self.at_punct(")"):
                args.append(self.parse_expr())
                while self.at_punct(","):
                    self.advance()
                    args.append(self.parse_expr())
            close_tok = None
            if self.at_punct(")"):
                close_tok = self.advance()
            else:
                self.diagnostics.append(
                    Diagnostic(
                        "unclosed '('", open_tok.start, open_tok.end
                    )
                )
            if close_tok is not None:
                end = close_tok.end
                closed = True
            elif args:
                end = args[-1].end
                closed = False
            else:
                end = open_tok.end
                closed = False
            expr = Node(
                "call",
                expr.start,
                end,
                {"closed_paren": closed},
                [expr] + args,
            )
        return expr

    def parse_primary(self) -> Node:
        t = self.cur()

        if t.kind == "NUMBER":
            self.advance()
            return Node("number", t.start, t.end, {"text": t.text})

        if t.kind == "STRING":
            self.advance()
            return Node(
                "string",
                t.start,
                t.end,
                {
                    "text": decode_string_literal(t.text),
                    "terminated": t.ok,
                },
            )

        if t.kind == "IDENT":
            self.advance()
            return Node("ident", t.start, t.end, {"name": t.text})

        if self.at_punct("("):
            open_tok = self.advance()
            inner = self.parse_expr()
            if self.at_punct(")"):
                close_tok = self.advance()
                return Node(
                    "paren",
                    open_tok.start,
                    close_tok.end,
                    {"closed_paren": True},
                    [inner],
                )
            self.diagnostics.append(
                Diagnostic("unclosed '('", open_tok.start, open_tok.end)
            )
            return Node(
                "paren",
                open_tok.start,
                inner.end,
                {"closed_paren": False},
                [inner],
            )

        # Missing expression.  Do not consume a synchronisation token so a
        # following ';' or ')' can still be matched normally.
        message = "expected expression"
        self.diagnostics.append(Diagnostic(message, t.start, t.start))
        return Node("error", t.start, t.start, {"message": message})
