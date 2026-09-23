"""Linear-time NFA simulation (the matching engine).

The input text is iterated as Python strings, i.e. sequences of **Unicode
code points** (``ord(ch)``); the engine never indexes UTF-16 code units.

Three matching modes are provided (documented in ``docs/language.md``):

* ``search``    (default) leftmost, and among matches at the same start
                position **longest** accepting match
* ``fullmatch`` the whole text must be one match
* ``prefix``    match starting at position 0 only, longest such match

Because the NFA is simulated by tracking the *set* of active states, the
runtime is ``O(text length × NFA states)`` with no backtracking — nested
quantifiers can never cause exponential behaviour.
"""
from __future__ import annotations

from dataclasses import dataclass

from .nfa import NFA
from .predicates import is_word_cp


@dataclass(frozen=True)
class Match:
    start: int          # code-point offset
    end: int            # code-point offset (exclusive)
    matched: str

    def as_dict(self) -> dict:
        return {"start": self.start, "end": self.end, "matched": self.matched}


class Simulator:
    def __init__(self, nfa: NFA, assertions: dict[int, list[str]]) -> None:
        self.nfa = nfa
        self.assertions = assertions

    # assertions ---------------------------------------------------------------
    def _check_assertions(self, state: int, text: str, pos: int) -> bool:
        for kind in self.assertions.get(state, ()):
            if kind == "^":
                if pos != 0:
                    return False
            elif kind == "$":
                if pos != len(text):
                    return False
            elif kind == "b":
                if self._word_boundary(text, pos) != 1:
                    return False
            elif kind == "B":
                if self._word_boundary(text, pos) == 1:
                    return False
            else:  # pragma: no cover - defensive
                return False
        return True

    @staticmethod
    def _word_boundary(text: str, pos: int) -> int:
        before = is_word_cp(ord(text[pos - 1])) if pos > 0 else False
        after = is_word_cp(ord(text[pos])) if pos < len(text) else False
        return int(before != after)

    # epsilon closure ----------------------------------------------------------
    def _closure(self, states: set[int], text: str, pos: int) -> set[int]:
        """Epsilon closure, honouring assertions at entry of each state."""
        result: set[int] = set()
        stack: list[int] = []
        for s in states:
            if self._check_assertions(s, text, pos) and s not in result:
                result.add(s)
                stack.append(s)
        while stack:
            s = stack.pop()
            for e in self.nfa.edges[s]:
                if e.kind != "eps":
                    continue
                t = e.target
                if t in result:
                    continue
                if not self._check_assertions(t, text, pos):
                    continue
                result.add(t)
                stack.append(t)
        return result

    # one character step -------------------------------------------------------
    def _step(self, current: set[int], cp: int) -> set[int]:
        nxt: set[int] = set()
        for s in current:
            for e in self.nfa.edges[s]:
                if e.kind == "char" and e.cp == cp:
                    nxt.add(e.target)
                elif e.kind == "pred" and e.predicate.matches(cp):  # type: ignore[union-attr]
                    nxt.add(e.target)
        return nxt

    def _run_from(self, text: str, start: int, anchored_start: bool) -> int | None:
        """Run from ``start``; return longest accepting end, or ``None``."""
        if anchored_start and not self._check_assertions(self.nfa.start, text, start):
            return None
        current = self._closure({self.nfa.start}, text, start)
        best = start if self.nfa.accept in current else None
        pos = start
        while pos < len(text):
            cp = ord(text[pos])
            nxt = self._step(current, cp)
            pos += 1
            current = self._closure(nxt, text, pos)
            if self.nfa.accept in current:
                best = pos
            if not current:
                break
        return best

    # public API ---------------------------------------------------------------
    def search(self, text: str) -> Match | None:
        for start in range(len(text) + 1):
            end = self._run_from(text, start, anchored_start=False)
            if end is not None:
                return Match(start, end, text[start:end])
        return None

    def fullmatch(self, text: str) -> Match | None:
        end = self._run_from(text, 0, anchored_start=True)
        if end == len(text):
            return Match(0, len(text), text)
        return None

    def prefix(self, text: str) -> Match | None:
        end = self._run_from(text, 0, anchored_start=True)
        if end is not None:
            return Match(0, end, text[:end])
        return None
