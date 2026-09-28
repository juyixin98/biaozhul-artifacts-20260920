#!/usr/bin/env python3
"""Build/rebuild one or all local indexes from synthetic corpora.

Examples
--------
    PYTHONPATH=src python3 scripts/reindex.py demo \
        --locale en_US --strength 3
    PYTHONPATH=src python3 scripts/reindex.py demo \
        --locale en_US --strength 3 --numeric
    PYTHONPATH=src python3 scripts/reindex.py demo --locale tr_TR --strength 3
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from collsvc.collation.options import (  # noqa: E402
    CASE_FIRST_DEFAULT,
    CollationOptions,
    VALID_CASE_FIRST,
    VALID_STRENGTHS,
)
from collsvc.config import Settings  # noqa: E402
from collsvc.corpus.loader import load_corpus  # noqa: E402
from collsvc.index.store import IndexStore  # noqa: E402


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Build a local collation index")
    p.add_argument("corpus", help="corpus fixture file stem (e.g. demo)")
    p.add_argument("--locale", default="en_US")
    p.add_argument("--strength", type=int, default=3, choices=sorted(VALID_STRENGTHS))
    p.add_argument("--numeric", action="store_true")
    p.add_argument("--case-first", default=CASE_FIRST_DEFAULT,
                   choices=sorted(VALID_CASE_FIRST))
    return p.parse_args()


def main() -> int:
    args = parse_args()
    settings = Settings.from_env()
    options = CollationOptions(
        locale=args.locale,
        strength=args.strength,
        numeric=args.numeric,
        case_first=args.case_first,
    )
    spec = load_corpus(settings.corpus_dir / f"{args.corpus}.json")

    import hashlib
    import json

    digest = hashlib.sha256(
        json.dumps(options.canonical_dict(), sort_keys=True).encode()
    ).hexdigest()[:16]
    db_path = settings.db_path.parent / "indexes" / f"idx_{digest}.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)

    store = IndexStore(db_path, options)
    result = store.build(spec, replace=True)
    print(f"built {result['row_count']} rows -> {db_path}")
    print(f"index_version: {result['index_version']}")
    print(f"mining stats: {result['mining']}")
    store.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
