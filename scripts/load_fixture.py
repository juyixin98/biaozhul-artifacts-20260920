"""Load a synthetic input fixture into the configured SQLite evidence store.

Usage::

    PYTHONPATH=src python scripts/load_fixture.py fixtures/graph_v1.json [--replace]
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from provenance.config import Settings  # noqa: E402
from provenance.evidence_store import EvidenceStore, Snapshot  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Load a versioned input fixture")
    parser.add_argument("fixture", type=Path, help="path to the fixture JSON file")
    parser.add_argument("--replace", action="store_true", help="replace an existing version")
    parser.add_argument("--db", type=Path, default=None, help="override SQLite database path")
    args = parser.parse_args()

    settings = Settings.from_env()
    db_path = args.db or settings.db_path
    raw = json.loads(args.fixture.read_text(encoding="utf-8"))
    snapshot = Snapshot.from_dict(raw)

    with EvidenceStore(db_path) as store:
        hashes = store.load_snapshot(snapshot, replace=args.replace)

    print(f"loaded version={snapshot.version} into {db_path}")
    rows_by_name = {rel.name: len(rel.rows) for rel in snapshot.relations}
    for name, digest in sorted(hashes.items()):
        print(f"  {name}: rows={rows_by_name[name]} sha256={digest[:16]}…")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
