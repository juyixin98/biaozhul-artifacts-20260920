"""npm ``workspaces`` glob modelling.

Supported, documented subset (npm 7+ ``workspaces`` field):

* plain directory names (``packages/a``);
* ``*`` matching path segments;
* ``**`` matching any number of path segments;
* trailing globs such as ``packages/*`` and ``libs/**/ui``.

Deliberately refused: negation (``!x``), character classes (``[abc]``),
question marks (``a?b``), brace expansion (``{a,b}``) and absolute paths.
These are not valid npm workspaces patterns anyway; refusing them makes the
failure explicit instead of silently matching nothing.
"""

from __future__ import annotations

import posixpath
import re
from dataclasses import dataclass

from .semver import UnsupportedRangeError  # re-used exception family


class WorkspacePatternError(ValueError):
    pass


@dataclass(frozen=True)
class WorkspaceSpec:
    raw: str
    regex: re.Pattern[str]
    negate: bool = False


def compile_pattern(raw: str) -> WorkspaceSpec:
    if not isinstance(raw, str) or not raw.strip():
        raise WorkspacePatternError("empty workspace pattern")
    pattern = raw.strip().replace("\\", "/")
    if pattern.startswith("/"):
        raise WorkspacePatternError(f"absolute workspace pattern {raw!r} is refused")
    if pattern.startswith("!"):
        raise WorkspacePatternError(
            f"negated workspace pattern {raw!r} is not supported by npm workspaces"
        )
    if any(token in pattern for token in ("[", "]", "?", "{")):
        raise WorkspacePatternError(
            f"workspace pattern {raw!r} uses character classes/braces/?; "
            "only *, ** and literal segments are supported"
        )

    segments: list[str] = []
    for segment in pattern.split("/"):
        if segment == "":
            raise WorkspacePatternError(
                f"workspace pattern {raw!r} contains an empty path segment"
            )
        if "**" in segment and segment != "**":
            raise WorkspacePatternError(
                f"workspace pattern {raw!r}: '**' must be a full path segment"
            )
        if segment == "**":
            segments.append("(?:[^/]+/)*[^/]+")
        elif "*" in segment:
            # single-segment star, no nested slashes
            escaped = re.escape(segment).replace(r"\*", "[^/]*")
            segments.append(escaped)
        else:
            segments.append(re.escape(segment))
    anchored = "^" + "/".join(segments) + "$"
    return WorkspaceSpec(raw=raw, regex=re.compile(anchored))


def expand_workspaces(
    declared: object, available_dirs: set[str]
) -> tuple[list[tuple[str, str]], list[str]]:
    """Resolve workspace globs against directories that exist in the bundle.

    Returns ``(matches, errors)`` where matches is a list of
    ``(pattern, relative_dir)`` sorted by directory path. ``available_dirs``
    is the set of POSIX directories that actually contain a ``package.json``.
    """
    if declared is None:
        return [], []
    if isinstance(declared, str):
        declared = [declared]
    if not isinstance(declared, list) or not all(isinstance(p, str) for p in declared):
        return [], ["'workspaces' must be an array of glob strings"]

    specs: list[WorkspaceSpec] = []
    errors: list[str] = []
    for raw in declared:
        try:
            specs.append(compile_pattern(raw))
        except WorkspacePatternError as exc:
            errors.append(str(exc))
    if errors:
        return [], errors

    matches: list[tuple[str, str]] = []
    seen_dirs: set[str] = set()
    for spec in specs:
        for directory in sorted(available_dirs):
            if spec.regex.match(directory) and directory not in seen_dirs:
                # A glob must match a directory directly: "packages/*" matches
                # "packages/a" but not "packages/a/deep".
                seen_dirs.add(directory)
                matches.append((spec.raw, directory))

    # A literal (glob-free) pattern that matches nothing is always an error.
    for spec in specs:
        literal = not any(ch in spec.raw for ch in "*")
        if literal and spec.raw not in {m[0] for m in matches}:
            errors.append(
                f"workspace path {spec.raw!r} has no package.json in the bundle"
            )
    return matches, errors


def workspace_dir_for_key(lock_key: str) -> str | None:
    """Return a workspace's directory for a v3 lock key not under node_modules."""
    if lock_key == "" or lock_key.startswith("node_modules/"):
        return None
    normalized = posixpath.normpath(lock_key)
    if normalized.startswith(".."):
        return None
    return normalized
