#!/usr/bin/env python3
"""End-to-end demonstration on the local synthetic fixture.

Run from the repository root:

    python3 scripts/demo.py

It loads ``fixtures/corpus.example.json``, resolves entities, locks a
human-confirmed mapping, re-resolves, and prints the affected entities and the
journal path used to replay the run.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from entity_resolution.clustering import SolverConfig
from entity_resolution.models import CorpusIn
from entity_resolution.service import EntityResolutionService
from entity_resolution.similarity import SimilarityConfig
from entity_resolution.storage import Storage


def main() -> None:
    fixture = json.loads((ROOT / "fixtures" / "corpus.example.json").read_text())
    corpus = CorpusIn.model_validate(fixture)

    service = EntityResolutionService(
        Storage(":memory:"),
        similarity=SimilarityConfig(
            threshold=0.45,
            hard_attribute_keys=frozenset({"reg_id"}),
        ),
        solver=SolverConfig(mode="auto"),
        log_dir=ROOT / "logs",
    )

    service.load_corpus(corpus)
    first = service.resolve(0.45)
    print(f"run {first.run_id}  threshold={first.threshold}")
    for cluster in first.clusters:
        flag = "LOCKED" if cluster.locked else "      "
        print(f"  [{flag}] {cluster.cluster_id}: {cluster.members}  ({cluster.canonical_name!r})")
    print("  rejected:")
    for rp in first.rejected_pairs:
        print(f"    {rp['pair']} score={rp['score']:.2f} -> {rp['reason']}")

    # A human confirms the Gazprom cross-language mapping and locks it.
    gaz = next(c for c in first.clusters if set(c.members) == {"r-010", "r-011"})
    service.lock_cluster("human-gazprom", gaz.members)
    second = service.resolve(0.45)
    print("\nafter locking human-gazprom:")
    for cluster in second.clusters:
        flag = "LOCKED" if cluster.locked else "      "
        print(f"  [{flag}] {cluster.cluster_id}: {cluster.members}")

    affected = service.affected_entities()
    print("\naffected entities (latest change):")
    print("  changed:", affected.changed_records)
    print("  created:", affected.clusters_created)
    journal = ROOT / "logs" / f"{second.run_id}.jsonl"
    print("\nreplay journal:", journal.relative_to(ROOT))


if __name__ == "__main__":
    main()
