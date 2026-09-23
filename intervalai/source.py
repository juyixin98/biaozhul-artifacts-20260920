"""Source locations and lexer tokens."""

from dataclasses import dataclass


@dataclass(frozen=True)
class Loc:
    """A half-open source span ``[start, end)`` with 1-based line/column.

    ``line`` / ``col`` point at the first character of the span.
    """

    line: int
    col: int
    start: int
    end: int

    def to_dict(self):
        return {"line": self.line, "col": self.col, "start": self.start, "end": self.end}

    @staticmethod
    def merge(a: "Loc", b: "Loc") -> "Loc":
        """Smallest span covering both (works even when they are out of order)."""
        s = min(a.start, b.start)
        e = max(a.end, b.end)
        return Loc(a.line if s == a.start else b.line,
                   a.col if s == a.start else b.col, s, e)


class Token:
    __slots__ = ("kind", "text", "loc")

    def __init__(self, kind, text, loc):
        self.kind = kind   # keyword/operator text verbatim, or one of ID/INT/EOF
        self.text = text
        self.loc = loc

    def __repr__(self):
        return f"Token({self.kind!r}, {self.text!r}, {self.loc.line}:{self.loc.col})"
