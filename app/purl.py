"""Package URL (purl) parsing — documented subset of RFC 9259.

Implements the real purl grammar: ``pkg:type/namespace/name@version?qualifiers#subpath``
with percent-decoding, lowercase normalization of type/namespace/name
(per-ecosystem rules), qualifier canonicalization and string assembly.
Invalid purls raise :class:`PurlError` (never silently "fixed").
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from urllib.parse import parse_qsl, quote, unquote

from .versions.errors import VersionError  # noqa: F401  (shared error family)


class PurlError(ValueError):
    """Raised when a purl string is structurally invalid."""


# Of the ecosystems this service understands, only Maven coordinates are
# case sensitive (groupId/artifactId). npm and PyPI names are lowercased.
_CASE_SENSITIVE = {"maven"}

_LEGAL_TYPE_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9.+-]*$")


@dataclass(frozen=True)
class Purl:
    type: str
    name: str
    namespace: tuple[str, ...] = ()
    version: str | None = None
    qualifiers: dict[str, str] = field(default_factory=dict)
    subpath: tuple[str, ...] = ()

    @property
    def ecosystem(self) -> str:
        # only the ecosystems this service has version rules for
        return self.type

    def canonical(self) -> str:
        """Rebuild the canonical purl string."""
        ns = "/".join(quote(s, safe="") for s in self.namespace)
        base = f"pkg:{self.type}/"
        if ns:
            base += ns + "/"
        base += quote(self.name, safe="")
        if self.version is not None:
            base += "@" + quote(self.version, safe="")
        if self.qualifiers:
            qs = "&".join(f"{k}={quote(v, safe='')}"
                          for k, v in sorted(self.qualifiers.items()))
            base += "?" + qs
        if self.subpath:
            base += "#" + "/".join(quote(s, safe="") for s in self.subpath)
        return base

    def variant_key(self) -> tuple:
        """Identity used to keep distinct *variants* from being merged."""
        return (self.type, self.namespace, self.name,
                tuple(sorted(self.qualifiers.items())), self.subpath)


def _decode_segment(seg: str, what: str) -> str:
    if seg == "":
        raise PurlError(f"empty {what} in purl")
    # reject malformed percent-escapes before unquote silently keeps them
    i = 0
    while i < len(seg):
        if seg[i] == "%":
            if i + 2 >= len(seg) or not re.match(r"^[0-9A-Fa-f]{2}$", seg[i + 1:i + 3]):
                raise PurlError(f"invalid percent-encoding in purl {what}: {seg!r}")
            i += 3
        else:
            i += 1
    # decoded separators ("/", "@" in npm scopes) are legal component
    # characters — the structural split already happened on raw text
    return unquote(seg)


def parse_purl(text: str) -> Purl:
    if not isinstance(text, str) or not text.strip():
        raise PurlError("purl must be a non-empty string")
    s = text.strip()
    if not s.startswith("pkg:"):
        raise PurlError("purl must start with 'pkg:' scheme")
    rest = s[4:]
    if rest.startswith("//"):
        # URL-style authority form is legal in RFC 9259
        rest = rest[2:]
    if not rest:
        raise PurlError("purl has no type/location")

    subpath: tuple[str, ...] = ()
    if "#" in rest:
        rest, sp = rest.split("#", 1)
        parts = [p for p in sp.split("/") if p not in ("", ".", "..")]
        subpath = tuple(_decode_segment(p, "subpath") for p in parts)
        if not subpath:
            raise PurlError("empty purl subpath")

    qualifiers: dict[str, str] = {}
    if "?" in rest:
        rest, q = rest.split("?", 1)
        for k, v in parse_qsl(q, keep_blank_values=True):
            k = k.lower()
            if not k or not re.match(r"^[a-zA-Z0-9.-]+$", k):
                raise PurlError(f"invalid purl qualifier key: {k!r}")
            if k in qualifiers:
                raise PurlError(f"duplicate purl qualifier key: {k!r}")
            qualifiers[k] = unquote(v)

    version = None
    if "@" in rest:
        rest, ver = rest.rsplit("@", 1)
        version = _decode_segment(ver, "version")
        if not version:
            raise PurlError("empty purl version")

    if "/" not in rest:
        raise PurlError("purl requires type/name at minimum")
    type_seg, _, path = rest.partition("/")
    ptype = type_seg.lower()
    if not _LEGAL_TYPE_RE.match(ptype):
        raise PurlError(f"invalid purl type: {type_seg!r}")
    if not path:
        raise PurlError("purl missing name")

    segs = path.split("/")
    name_seg = segs.pop()
    if not name_seg:
        raise PurlError("purl missing name")
    namespace = tuple(_decode_segment(s, "namespace") for s in segs if s)
    name = _decode_segment(name_seg, "name")

    if ptype not in _CASE_SENSITIVE:
        namespace = tuple(ns.lower() for ns in namespace)
        name = name.lower()

    return Purl(type=ptype, name=name, namespace=namespace, version=version,
                qualifiers=qualifiers, subpath=subpath)
