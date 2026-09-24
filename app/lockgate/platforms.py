"""Platform / engine constraint evaluation for optional dependencies.

npm marks a dependency optional when it may legitimately be absent on some
targets. A node carries ``os``, ``cpu`` and ``libc`` allow/deny lists; we
evaluate them against an explicit target triple supplied by the caller
(default: the host running the gate is *not* assumed).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional

# Node's supported values, mirrored from
# https://docs.npmjs.com/cli/v10/configuring-npm/package-json#os
_KNOWN_OS = {"aix", "darwin", "freebsd", "linux", "openbsd", "sunos", "win32", "android"}
_KNOWN_CPU = {"arm", "arm64", "ia32", "mips", "mipsel", "ppc", "ppc64", "s390", "s390x", "x64", "x32"}
_KNOWN_LIBC = {"glibc", "musl"}


@dataclass(frozen=True)
class Target:
    os: str
    cpu: str
    libc: Optional[str] = None


def _match_list(values: List[str], actual: Optional[str], known: set[str], kind: str) -> Optional[str]:
    """Return None when satisfied, otherwise a human-readable reason."""
    if not values:
        return None
    allowed, denied = [], []
    for v in values:
        if not isinstance(v, str) or not v:
            return f"malformed {kind} constraint entry: {v!r}"
        if v.startswith("!"):
            denied.append(v[1:])
        else:
            allowed.append(v)
    for v in (*allowed, *denied):
        if v not in known:
            return f"unknown {kind} constraint value: {v!r}"
    if actual is None and (allowed or denied):
        return f"package requires {kind} constraint {values!r} but target provides none"
    if denied and actual in denied:
        return f"target {kind}={actual} is explicitly excluded by {values!r}"
    if allowed and actual not in allowed:
        return f"target {kind}={actual} is not in allowed list {values!r}"
    return None


def evaluate(node: dict, target: Target) -> Optional[str]:
    """Return None if node supports target, else the exclusion reason."""
    for fn in (
        _match_list(node.get("os", []), target.os, _KNOWN_OS, "os"),
        _match_list(node.get("cpu", []), target.cpu, _KNOWN_CPU, "cpu"),
        _match_list(node.get("libc", []), target.libc, _KNOWN_LIBC, "libc"),
    ):
        if fn:
            return fn
    return None
