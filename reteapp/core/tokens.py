"""Working-memory elements and beta tokens.

Identity semantics (explicit, tested):

* Every successful :meth:`Engine.insert_fact` mints a **new monotonic WME id**.
* Two structurally identical facts are *distinct* WMEs: both coexist in
  working memory and each independently joins. Value equality is used for
  variable joins; the WME id is the token identity. This mirrors the default
  CLIPS/Jess duplicate-fact behaviour and makes "repeat insert" unambiguous.
* A :class:`Token` is an ordered tuple of WME ids (one per condition so far)
  plus the accumulated variable bindings. Token keys are deterministic
  ``(parent_key, wme_id)`` pairs so the same logical partial match is stored
  exactly once in a beta memory.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping

# Sentinel token key for the dummy (zero-condition) beta root.
DUMMY_KEY: tuple[()] = ()


@dataclass(frozen=True)
class WME:
    id: int
    type: str
    fields: Mapping[str, Any]
    # Stable content hash used only for display/diagnostics; never identity.
    content_key: str


@dataclass(frozen=True, eq=False)
class Token:
    """A partial or complete match held in a beta memory.

    Identity is the ordered physical WME tuple (``key``), never the bindings
    mapping - two tokens over the same facts are the same token even if a
    binding representation differed.
    """

    key: tuple
    wme_ids: tuple[int, ...]
    bindings: Mapping[str, Any] = field(default_factory=dict)
    parent_key: tuple | None = None

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, Token):
            return NotImplemented
        return self.key == other.key

    def __hash__(self) -> int:
        return hash(self.key)
