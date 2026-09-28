"""Longest-match lexer over the combined DFA.

Token selection rule (fixed by the corpus specification):

1. **Longest match first** — the token is the longest prefix of the remaining
   input accepted by any rule.
2. **Explicit priority** — among rules accepting that longest prefix, the
   lowest ``(priority, declaration index)`` wins.

Because rules that can match the empty string are rejected at compile time,
every emitted token consumes at least one character and the scan always
terminates.
"""

from __future__ import annotations

from dataclasses import dataclass

from .compiler import CompiledLexer
from .dfa import step


@dataclass(frozen=True)
class Token:
    rule: str
    text: str
    start: int
    end: int

    def to_dict(self) -> dict[str, object]:
        return {"rule": self.rule, "text": self.text, "start": self.start, "end": self.end}


@dataclass(frozen=True)
class LexErrorInfo:
    offset: int
    char: str
    message: str

    def to_dict(self) -> dict[str, object]:
        return {
            "category": "INPUT_ERROR",
            "offset": self.offset,
            "char": self.char,
            "message": self.message,
        }


def lex(compiled: CompiledLexer, text: str) -> tuple[list[Token], LexErrorInfo | None]:
    dfa = compiled.dfa
    rules = compiled.rules
    tokens: list[Token] = []
    pos = 0
    length = len(text)
    while pos < length:
        state = dfa.start
        best_rule: int | None = None
        best_end = -1
        cursor = pos
        while cursor < length:
            nxt = step(dfa, state, ord(text[cursor]))
            if nxt is None:
                break
            state = nxt
            cursor += 1
            accepts = dfa.accepts[state]
            if accepts:
                best_rule = min(
                    accepts, key=lambda idx: (rules[idx].priority, idx)
                )
                best_end = cursor
        if best_rule is None:
            bad = text[pos]
            return tokens, LexErrorInfo(
                offset=pos,
                char=bad,
                message=(
                    f"no token rule matches at offset {pos} "
                    f"(character {bad!r}, U+{ord(bad):04X})"
                ),
            )
        tokens.append(
            Token(
                rule=rules[best_rule].name,
                text=text[pos:best_end],
                start=pos,
                end=best_end,
            )
        )
        pos = best_end
    return tokens, None
