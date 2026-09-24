"""Local vulnerability fixtures.

The data shipped in ``data/vulnerabilities.json`` is a hand-maintained,
explicitly *local* fixture. It is NOT a feed and makes no claim of
covering current or complete vulnerability information — there is no
network access anywhere in this service.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

DEFAULT_FIXTURE = Path(__file__).resolve().parent.parent / "data" / "vulnerabilities.json"


@dataclass(frozen=True)
class AffectedRange:
    range_id: str
    ecosystem: str
    expression: str
    introduced: str | None
    fixed: str | None
    source_note: str


@dataclass(frozen=True)
class Advisory:
    vulnerability_id: str
    ecosystem: str
    name: str
    namespace: tuple[str, ...]
    summary: str
    severity: str
    ranges: tuple[AffectedRange, ...]

    def package_key(self) -> tuple:
        return (self.ecosystem, self.namespace, self.name)


def load_advisories(path: str | Path | None = None) -> tuple[list[Advisory], dict]:
    fixture_path = Path(path) if path else DEFAULT_FIXTURE
    with fixture_path.open("r", encoding="utf-8") as fh:
        doc = json.load(fh)
    if not isinstance(doc, dict) or "advisories" not in doc:
        raise ValueError(f"fixture {fixture_path} must be an object with 'advisories'")

    out: list[Advisory] = []
    seen_ids: set[str] = set()
    for i, adv in enumerate(doc["advisories"]):
        ctx = f"advisories[{i}]"
        vid = adv["id"]
        if vid in seen_ids:
            raise ValueError(f"{ctx}: duplicate advisory id {vid!r}")
        seen_ids.add(vid)
        ranges = tuple(
            AffectedRange(
                range_id=r.get("id", f"{vid}:range{j}"),
                ecosystem=r["ecosystem"],
                expression=r["expression"],
                introduced=r.get("introduced"),
                fixed=r.get("fixed"),
                source_note=r.get("note", "local fixture; not a live feed"),
            )
            for j, r in enumerate(adv.get("ranges", []))
        )
        out.append(Advisory(
            vulnerability_id=vid,
            ecosystem=adv["ecosystem"],
            name=adv["name"],
            namespace=tuple(adv.get("namespace", [])),
            summary=adv.get("summary", ""),
            severity=adv.get("severity", "UNSPECIFIED"),
            ranges=ranges,
        ))
    meta = doc.get("metadata", {})
    meta.setdefault("source", "local fixture shipped with this repository")
    meta.setdefault("live_feed", False)
    return out, meta
