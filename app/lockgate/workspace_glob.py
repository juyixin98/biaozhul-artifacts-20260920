"""Minimal, explicitly-bounded workspace glob matcher.

npm workspaces accept patterns like ``packages/*`` and ``apps/**/lib``.
Full node-glob semantics (negation groups, brace expansion, extglobs,
character classes, ...) are intentionally **not** supported — they add
confusion without appearing in real workspace manifests. Unsupported
syntax is reported via :class:`UnsupportedGlobError`.
"""
from __future__ import annotations

import re
from typing import Iterable, List


class UnsupportedGlobError(ValueError):
    pass


def _translate(segment: str) -> str:
    out = []
    i = 0
    while i < len(segment):
        ch = segment[i]
        if ch == "*":
            if i + 1 < len(segment) and segment[i + 1] == "*":
                raise UnsupportedGlobError(
                    "'**' may only appear as a complete path segment"
                )
            # single '*' does not cross '/'
            out.append("[^/]*")
            i += 1
        elif ch == "?":
            out.append("[^/]")
            i += 1
        elif ch in "[](){}!+@\\":
            raise UnsupportedGlobError(
                f"unsupported glob character {ch!r} in workspace pattern"
            )
        else:
            out.append(re.escape(ch))
            i += 1
    return "".join(out)


def compile_pattern(pattern: str) -> re.Pattern:
    p = pattern.strip().strip("/")
    if not p:
        raise UnsupportedGlobError("empty workspace pattern")
    if p.startswith("!"):
        raise UnsupportedGlobError("negated workspace patterns are not supported")
    regex_segments: list[str] = []
    for seg in p.split("/"):
        if seg == "**":
            regex_segments.append("(?:[^/]+/)*")  # zero+ complete segments
        elif "**" in seg:
            raise UnsupportedGlobError("'**' may only appear as a complete segment")
        else:
            regex_segments.append(_translate(seg) + "/")
    # trailing "/" after the final literal segment is removed below
    body = "".join(regex_segments).rstrip("/")
    return re.compile("^" + body + "$")


def matches(path: str, patterns: Iterable[str]) -> bool:
    norm = path.strip().strip("/")
    for pat in patterns:
        if compile_pattern(pat).match(norm):
            return True
    return False


def expand(patterns: List[str], available_dirs: List[str]) -> List[str]:
    """Return sorted de-duplicated dirs from ``available_dirs`` matching any pattern."""
    out = set()
    for pat in patterns:
        rx = compile_pattern(pat)
        for d in available_dirs:
            if rx.match(d.strip("/")):
                out.add(d.strip("/"))
    return sorted(out)
