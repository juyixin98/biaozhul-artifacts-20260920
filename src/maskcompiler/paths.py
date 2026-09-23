"""JSONPath-subset parser and matcher.

Supported grammar (no leading/trailing whitespace allowed inside a path)::

    path            := "$" segment*
    segment         := child | wildcard | bracket | recursive
    child           := "." bare-name
    wildcard        := ".*"
    bracket         := "[" "*" | int | quoted-string "]"
    recursive       := ".." ( bare-name | "*" | bracket )
    bare-name       := any run of chars outside  . [ ] * ' " and whitespace
    quoted-string   := "'" ( "''" | any char except ' )* "'
                      (double-quoted strings are accepted too, with "" escaping)

Semantics
---------
``$.a.b``       object children
``$..name``     recursive descent to every ``name`` key at any depth
``$.users[*]``  every element of an array
``$..[*]``      every array element at any depth
``$.items[0]``  a specific array index (negative indices count from the end)
``$['x-y']``    quoted key (needed for names with special characters)
``$``           the document root

``[*]`` on an object yields its values (standard JSONPath). A segment only
matches where its structural precondition holds: a key segment on a
non-object, or an index on a non-array, simply produces no matches instead
of raising -- paths are selective, not assertions.
"""

from typing import Any, List, Tuple

from .errors import PathSyntaxError

# A location is the chain of steps from the document root: a string is an
# object key, an int is an array index. The empty tuple is the root.
Location = Tuple[Any, ...]

# Token kinds:
#   ("key", name)
#   ("index", i)
#   ("any",)        .*  or [*]  -- any child/element of an object/array
#   ("rkey", name)  ..name
#   ("rany",)       ..[*] / ..*
Token = Tuple[Any, ...]

_BARE_STOP = set(".[]*'\" \t\r\n")
_BARE_STOP |= {chr(0x0B), chr(0x0C)}


def parse_path(path: str) -> List[Token]:
    if not isinstance(path, str):
        raise PathSyntaxError("path must be a string")
    if path != path.strip() or not path:
        raise PathSyntaxError("path must be a non-empty string without surrounding whitespace")
    if not path.startswith("$"):
        raise PathSyntaxError("path must start with '$'")
    pos = 1
    tokens: List[Token] = []
    n = len(path)
    while pos < n:
        ch = path[pos]
        if ch == ".":
            recursive = pos + 1 < n and path[pos + 1] == "."
            pos += 2 if recursive else 1
            if pos >= n:
                raise PathSyntaxError("'.' must be followed by a name, '*' or '[]'")
            if path[pos] == "*":
                tokens.append(("rany",) if recursive else ("any",))
                pos += 1
            elif path[pos] == "[":
                if not recursive:
                    raise PathSyntaxError("unexpected '[' after '.'")
                pos, tok = _read_bracket(path, pos)
                if tok[0] != "any":
                    raise PathSyntaxError("only '[*]' is supported after '..'")
                tokens.append(("rany",))
            else:
                name, pos = _read_bare(path, pos)
                tokens.append(("rkey", name) if recursive else ("key", name))
        elif ch == "[":
            pos, tok = _read_bracket(path, pos)
            tokens.append(tok)
        else:
            raise PathSyntaxError("expected '.', '[' or end of path at position %d" % pos)
    return tokens


def _read_bare(path: str, pos: int) -> Tuple[str, int]:
    start = pos
    n = len(path)
    while pos < n and path[pos] not in _BARE_STOP:
        pos += 1
    if pos == start:
        raise PathSyntaxError("expected a name at position %d" % pos)
    return path[start:pos], pos


def _read_quoted(path: str, pos: int) -> Tuple[str, int]:
    quote = path[pos]
    pos += 1
    out: List[str] = []
    n = len(path)
    while pos < n:
        ch = path[pos]
        if ch == quote:
            if pos + 1 < n and path[pos + 1] == quote:
                out.append(quote)
                pos += 2
                continue
            return "".join(out), pos + 1
        out.append(ch)
        pos += 1
    raise PathSyntaxError("unterminated quoted string in path")


def _read_bracket(path: str, pos: int) -> Tuple[int, Token]:
    # path[pos] == '['
    pos += 1
    n = len(path)
    if pos >= n:
        raise PathSyntaxError("unterminated '[' in path")
    ch = path[pos]
    if ch == "*":
        pos += 1
        if pos >= n or path[pos] != "]":
            raise PathSyntaxError("expected ']' after '*'")
        return pos + 1, ("any",)
    if ch in ("'", '"'):
        name, pos = _read_quoted(path, pos)
        if pos >= n or path[pos] != "]":
            raise PathSyntaxError("expected ']' after quoted key")
        return pos + 1, ("key", name)
    start = pos
    if ch == "-":
        pos += 1
    while pos < n and path[pos].isdigit():
        pos += 1
    if pos == start or (pos == start + 1 and ch == "-"):
        raise PathSyntaxError("expected '*', a quoted key or an integer inside '[]'")
    index = int(path[start:pos])
    if pos >= n or path[pos] != "]":
        raise PathSyntaxError("expected ']' after array index")
    return pos + 1, ("index", index)


def _iter_children(node: Any):
    """Yield ``(step, child)`` for direct children of an object/array."""
    if isinstance(node, dict):
        for k, v in node.items():
            yield k, v
    elif isinstance(node, list):
        for i, v in enumerate(node):
            yield i, v


def _iter_descendants(node: Any, base: Location):
    """Yield ``(location, value)`` for every strict descendant."""
    for step, child in _iter_children(node):
        child_loc = base + (step,)
        yield child_loc, child
        if isinstance(child, (dict, list)):
            yield from _iter_descendants(child, child_loc)


def _apply_suffix(rest: List[Token], value: Any, loc: Location):
    return _match_suffix(rest, value, loc)


def _match_suffix(tokens: List[Token], node: Any, loc: Location):
    """Apply ``tokens`` starting at ``node``.

    A recursive token (``rkey``/``rany``) tries its *whole remaining suffix*
    both at matching children of this node and at every descendant node --
    that is what descent means.
    """
    if not tokens:
        return [(loc, node)]
    tok = tokens[0]
    rest = tokens[1:]
    kind = tok[0]
    out: List[Tuple[Location, Any]] = []

    if kind == "key":
        if isinstance(node, dict) and tok[1] in node:
            out.extend(_apply_suffix(rest, node[tok[1]], loc + (tok[1],)))
    elif kind == "index":
        if isinstance(node, list) and node and -len(node) <= tok[1] < len(node):
            real = tok[1] if tok[1] >= 0 else len(node) + tok[1]
            out.extend(_apply_suffix(rest, node[real], loc + (real,)))
    elif kind == "any":
        for step, child in _iter_children(node):
            out.extend(_apply_suffix(rest, child, loc + (step,)))
    elif kind == "rkey":
        # The key at this node...
        if isinstance(node, dict) and tok[1] in node:
            out.extend(_apply_suffix(rest, node[tok[1]], loc + (tok[1],)))
        # ...and the same suffix retried from every strict descendant.
        for desc_loc, desc in _iter_descendants(node, loc):
            if isinstance(desc, dict) and tok[1] in desc:
                out.extend(_apply_suffix(rest, desc[tok[1]], desc_loc + (tok[1],)))
    elif kind == "rany":
        # Every element of this node (if an array)...
        if isinstance(node, list):
            for i, v in enumerate(node):
                out.extend(_apply_suffix(rest, v, loc + (i,)))
        # ...and the suffix retried from every strict descendant, so a
        # trailing segment (e.g. another [*]) is applied at each array found
        # during the descent.
        for desc_loc, desc in _iter_descendants(node, loc):
            if isinstance(desc, list):
                for i, v in enumerate(desc):
                    out.extend(_apply_suffix(rest, v, desc_loc + (i,)))
    return out


def match(tokens: List[Token], document: Any) -> List[Tuple[Location, Any]]:
    """Return ``(location, value)`` pairs for every match of ``tokens``."""
    return _match_suffix(tokens, document, ())


def render_location(loc: Location) -> str:
    """Canonical, data-free rendering of a location for error messages."""
    out = "$"
    for step in loc:
        if isinstance(step, int):
            out += "[%d]" % step
        elif _is_bare(str(step)):
            out += "." + str(step)
        else:
            out += "[" + _quote_key(str(step)) + "]"
    return out


def _is_bare(name: str) -> bool:
    return bool(name) and all(c not in _BARE_STOP for c in name)


def _quote_key(name: str) -> str:
    return "'" + name.replace("'", "''") + "'"
