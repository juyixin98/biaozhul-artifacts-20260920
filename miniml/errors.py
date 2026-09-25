"""Render lexer / parser / inference errors with source locations.

Output looks like::

    error[E002]: type mismatch ...
      --> <input>:3:14
       |
     3 | let x = 1 + true ;;
       |              ^^^^ expected int, but got bool
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from .span import Span


def _line_starts(src: str) -> list[int]:
    starts = [0]
    for i, c in enumerate(src):
        if c == "\n":
            starts.append(i + 1)
    return starts


def _line_index(starts: list[int], offset: int) -> int:
    # largest line start <= offset
    lo, hi = 0, len(starts) - 1
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if starts[mid] <= offset:
            lo = mid
        else:
            hi = mid - 1
    return lo


@dataclass
class _Marker:
    line: int
    col_start: int
    col_end: int  # exclusive, clamped to within the line
    label: str
    primary: bool


def _markers_for_span(src: str, starts: list[int], span: Span,
                      label: str, primary: bool) -> list[_Marker]:
    li = _line_index(starts, span.lo)
    end_line = _line_index(starts, max(span.lo, span.hi - 1))
    out: list[_Marker] = []
    for ln in range(li, end_line + 1):
        line_lo = starts[ln]
        line_hi = src.find("\n", line_lo)
        if line_hi == -1:
            line_hi = len(src)
        cs = max(span.lo, line_lo) - line_lo + 1
        ce = min(span.hi, line_hi) - line_lo + 1
        ce = max(ce, cs + 1)  # zero-width spans still get one caret
        cs = min(cs, line_hi - line_lo + 1)
        out.append(_Marker(ln + 1, cs, ce, label if ln == end_line else "", primary))
    return out


def render_error(
    src: str,
    message: str,
    span: Span,
    filename: str = "<input>",
    code: str = "E000",
    related: Optional[list[tuple[Span, str]]] = None,
) -> str:
    starts = _line_starts(src)
    lines = src.split("\n")
    markers = _markers_for_span(src, starts, span, message, True)
    for rel_span, rel_label in related or []:
        markers += _markers_for_span(src, starts, rel_span, rel_label, False)

    gutter_w = max(len(str(m.line)) for m in markers)
    first = markers[0]
    out = [f"error[{code}]: {message}", f"{(' ' * (gutter_w - 1))}--> {filename}:{first.line}:{first.col_start}"]
    out.append(" " * gutter_w + " |")

    # Group markers by line so multi-span diagnostics share a line block.
    by_line: dict[int, list[_Marker]] = {}
    for m in markers:
        by_line.setdefault(m.line, []).append(m)

    for line_no in sorted(by_line):
        src_line = lines[line_no - 1] if 0 < line_no <= len(lines) else ""
        out.append(f"{line_no:>{gutter_w}} | {src_line}")
        annot = ""
        for m in by_line[line_no]:
            start = m.col_start - 1
            width = max(1, m.col_end - m.col_start)
            caret = "^" if m.primary else "-"
            if start >= len(annot):
                annot += " " * (start - len(annot)) + caret * width
            else:
                annot = annot[:start] + caret * width + annot[start + width:]
        labels = [m.label for m in by_line[line_no] if m.label]
        line = " " * gutter_w + " | " + annot
        if labels:
            line += " " + "; ".join(labels)
        out.append(line)
        out.append(" " * gutter_w + " |")

    return "\n".join(out)


def render_compile_error(src: str, err: Exception, filename: str = "<input>") -> str:
    from .lexer import LexError
    from .parser import ParseError
    from .infer import InferError

    if isinstance(err, LexError):
        return render_error(src, err.message, err.span, filename, code="E001")
    if isinstance(err, ParseError):
        return render_error(src, err.message, err.span, filename, code="E002")
    if isinstance(err, InferError):
        return render_error(
            src,
            err.message,
            err.span,
            filename,
            code="E003" if not err.cycle else "E004",
            related=err.related,
        )
    raise err
