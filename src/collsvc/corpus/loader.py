"""Load corpus fixtures from disk."""
from __future__ import annotations

import json
from pathlib import Path

from .spec import CorpusEntry, CorpusSpec, InvalidCorpusSpec


def load_corpus(path: Path) -> CorpusSpec:
    if not path.exists():
        raise FileNotFoundError(f"corpus file not found: {path}")
    return CorpusSpec.load_file(path)


def load_corpus_bundle(path: Path) -> tuple[CorpusSpec, ...]:
    """Load either a single corpus or a ``{"corpora": [...]}`` bundle."""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if isinstance(data, dict) and "corpora" in data:
        specs = tuple(CorpusSpec.from_dict(raw) for raw in data["corpora"])
        names = [s.name for s in specs]
        dupes = sorted({n for n in names if names.count(n) > 1})
        if dupes:
            raise InvalidCorpusSpec(f"duplicate corpus names in bundle: {dupes}")
        return specs
    return (CorpusSpec.from_dict(data),)


def entries_from_texts(name: str, texts: list[str]) -> CorpusSpec:
    """Convenience builder for ad-hoc/test corpora."""
    return CorpusSpec(
        name=name,
        description="in-memory corpus",
        entries=tuple(
            CorpusEntry(doc_id=f"t-{i:04d}", text=text)
            for i, text in enumerate(texts)
        ),
    )
