"""Document state and the incremental parse driver.

A :class:`Document` keeps the current source text together with the last
parse result (tokens, tree, diagnostics, and the token range of each
top-level declaration).  On :meth:`Document.apply_edit`:

1. The source is spliced at the edit point.
2. The *whole* input is re-lexed (lexing is cheap and the natural place to
   re-discover tokens after edits inside strings/comments).
3. The parser is restarted, but each old declaration whose span does not
   intersect the edit is offered as a *reuse candidate*.  The candidate is
   accepted only if its token slice -- kind, lexeme, string-termination
   flag and relative offsets -- matches the freshly lexed tokens exactly
   (see :meth:`minilang.parser.Parser._try_reuse`).  Accepted nodes keep
   their Python identity; nodes after the edit have their offsets shifted.
4. Everything that changed or whose lexical context changed is reparsed
   with the same recursive-descent parser used for full parses.

Therefore an accepted candidate is byte-for-byte identical (modulo an
offset translation) to what a full parse would produce at the new
position.  The test-suite fuzzes edits to prove this.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from .lexer import Diagnostic, Token, compute_line_starts, lex
from .nodes import Node
from .parser import DeclRange, Parser


@dataclass
class ParseResult:
    text: str
    tokens: List[Token]
    tree: Node
    diagnostics: List[Diagnostic]
    decl_ranges: List[DeclRange]
    decl_diagnostics: List[List[Diagnostic]] = field(default_factory=list)
    line_starts: List[int] = field(default_factory=list)
    stats: Dict[str, int] = field(default_factory=dict)


def full_parse(text: str) -> ParseResult:
    """Lex and parse *text* from scratch (no reuse)."""
    tokens, lex_diagnostics = lex(text)
    parser = Parser(tokens, len(text))
    tree = parser.parse_program()
    return ParseResult(
        text=text,
        tokens=tokens,
        tree=tree,
        diagnostics=lex_diagnostics + parser.diagnostics,
        decl_ranges=parser.decl_ranges,
        decl_diagnostics=parser.decl_diagnostics,
        line_starts=compute_line_starts(text),
        stats={"reused": 0, "parsed": parser.parsed},
    )


class EditError(ValueError):
    """Raised for edit ranges outside the current document."""


class Document:
    """Editable source text with incrementally maintained parse state."""

    def __init__(self, text: str = "") -> None:
        self.result = full_parse(text)

    @property
    def text(self) -> str:
        return self.result.text

    def apply_edit(
        self, start: int, old_len: int, new_text: str
    ) -> ParseResult:
        """Replace ``text[start:start + old_len]`` with *new_text*.

        ``old_len`` may be zero (pure insertion); ``new_text`` may be empty
        (pure deletion).  Returns the new :class:`ParseResult` and stores it
        on ``self.result`` for the next edit.

        Note: reused declaration subtrees are shifted in place, so objects
        from the previous result may now carry the *new* offsets.
        """
        old = self.result
        text = old.text
        if not isinstance(new_text, str):
            raise EditError("new_text must be a string")
        if start < 0 or old_len < 0 or start + old_len > len(text):
            raise EditError(
                f"edit range [{start}, {start + old_len}) is outside "
                f"document of length {len(text)}"
            )

        new_source = text[:start] + new_text + text[start + old_len:]
        delta = len(new_text) - old_len
        new_tokens, lex_diagnostics = lex(new_source)

        # Map old declaration nodes to their position in the new token
        # stream.  Declarations intersecting the edit cannot be reused.
        token_start_index = {t.start: i for i, t in enumerate(new_tokens)}
        candidates: Dict[int, list] = {}
        old_end = start + old_len
        for index, (node, tok0, tok1) in enumerate(old.decl_ranges):
            if node.end <= start:
                node_delta = 0  # entirely before the edit
            elif node.start >= old_end:
                node_delta = delta  # entirely after the edit
            else:
                continue  # overlaps the edit -> reparse
            new_start = node.start + node_delta
            new_index = token_start_index.get(new_start)
            if new_index is None:
                continue  # first token vanished or merged (e.g. in a string)
            decl_diags = old.decl_diagnostics[index]
            # A diagnostic at or past the node end points at the lookahead
            # token (e.g. "expected ';'"); reuse then also requires the
            # lookahead to sit at the same relative offset.
            lookahead_sensitive = any(
                d.start >= node.end for d in decl_diags
            )
            candidates.setdefault(new_start, []).append(
                (
                    node,
                    old.tokens,
                    tok0,
                    tok1,
                    node_delta,
                    decl_diags,
                    lookahead_sensitive,
                )
            )

        parser = Parser(new_tokens, len(new_source))
        tree = parser.parse_program(candidates if candidates else None)
        result = ParseResult(
            text=new_source,
            tokens=new_tokens,
            tree=tree,
            diagnostics=lex_diagnostics + parser.diagnostics,
            decl_ranges=parser.decl_ranges,
            decl_diagnostics=parser.decl_diagnostics,
            line_starts=compute_line_starts(new_source),
            stats={"reused": parser.reused, "parsed": parser.parsed},
        )
        self.result = result
        return result
