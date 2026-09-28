"""Corpus specification.

A corpus spec declares *what* synthetic material should exist and its provenance
(fixture file or generated generator). Specs are validated at the boundary —
a malformed spec fails fast with a categorical error instead of producing a
half-built index.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")


@dataclass(frozen=True)
class CorpusEntry:
    """One string with a stable external identity.

    ``text`` is the raw original; it is never rewritten (NFC/NFD forms stay
    distinct as documents even when their sort keys coincide).
    """

    doc_id: str
    text: str
    tags: frozenset[str] = field(default_factory=frozenset)

    def __post_init__(self) -> None:
        if not isinstance(self.doc_id, str) or not self.doc_id:
            raise InvalidCorpusSpec("doc_id must be a non-empty string")
        if not isinstance(self.text, str):
            raise InvalidCorpusSpec(
                f"entry {self.doc_id!r}: text must be a string"
            )
        object.__setattr__(self, "tags", frozenset(self.tags))


@dataclass(frozen=True)
class CorpusSpec:
    name: str
    description: str
    entries: tuple[CorpusEntry, ...]

    def __post_init__(self) -> None:
        if not _ID_RE.match(self.name):
            raise InvalidCorpusSpec(
                f"corpus name must match {_ID_RE.pattern}, got {self.name!r}"
            )
        if not self.entries:
            raise InvalidCorpusSpec(f"corpus {self.name!r} has no entries")
        ids = [e.doc_id for e in self.entries]
        dupes = sorted({x for x in ids if ids.count(x) > 1})
        if dupes:
            raise InvalidCorpusSpec(
                f"corpus {self.name!r} contains duplicate doc_id values: {dupes[:5]}"
            )

    @staticmethod
    def from_dict(raw: dict[str, Any]) -> "CorpusSpec":
        missing = {"name", "entries"} - set(raw)
        if missing:
            raise InvalidCorpusSpec(f"corpus spec missing keys: {sorted(missing)}")
        entries = []
        for i, item in enumerate(raw["entries"]):
            if isinstance(item, str):
                item = {"doc_id": f"auto-{i:04d}", "text": item}
            if not isinstance(item, dict) or "text" not in item:
                raise InvalidCorpusSpec(
                    f"entry #{i} must be a string or object with 'text'"
                )
            entries.append(
                CorpusEntry(
                    doc_id=str(item.get("doc_id") or f"auto-{i:04d}"),
                    text=item["text"],
                    tags=frozenset(item.get("tags", ())),
                )
            )
        return CorpusSpec(
            name=raw["name"],
            description=str(raw.get("description", "")),
            entries=tuple(entries),
        )

    @staticmethod
    def load_file(path: Path) -> "CorpusSpec":
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        if isinstance(data, dict) and "corpora" in data:
            raise InvalidCorpusSpec(
                f"{path}: expected a single corpus, got a corpora bundle"
            )
        spec = CorpusSpec.from_dict(data)
        if spec.name != path.stem:
            raise InvalidCorpusSpec(
                f"{path}: corpus name {spec.name!r} must equal file stem {path.stem!r}"
            )
        return spec


class InvalidCorpusSpec(ValueError):
    """Malformed corpus specification."""
