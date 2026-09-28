"""Load the bundled synthetic fixtures into a running service.

Usage::

    python scripts/load_fixtures.py [--db data/wfst.sqlite3] [--base-url URL]

Default mode writes straight into the SQLite index (no server needed).
With ``--base-url`` it POSTs each corpus to the HTTP ``/corpora/.../load``
endpoint instead.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from wfst_service.index.repository import IndexRepository  # noqa: E402
from wfst_service.index.service import IndexService  # noqa: E402

FIXTURE_FILES = [
    ("demo", "demo_corpus.json"),
]
NEGATIVE_FILES = [
    ("bad_epsilon", "invalid_epsilon_cycle.json"),
    ("bad_negative", "invalid_negative_cycle.json"),
]


def load_direct(db_path: str) -> None:
    repo = IndexRepository(db_path)
    service = IndexService(repo)
    for corpus_id, filename in FIXTURE_FILES:
        report = service.load_corpus_file(
            corpus_id, ROOT / "fixtures" / "corpora" / filename
        )
        print(f"[loaded] {corpus_id}")
        for line in report.as_lines():
            print(line)
    for corpus_id, filename in NEGATIVE_FILES:
        try:
            service.load_corpus_file(
                corpus_id, ROOT / "fixtures" / "corpora" / filename
            )
        except Exception as exc:  # noqa: BLE001 - demonstration of rejection
            print(f"[correctly rejected] {corpus_id}: {exc}")
    repo.close()


def load_http(base_url: str) -> None:
    for corpus_id, filename in FIXTURE_FILES:
        doc = json.loads(
            (ROOT / "fixtures" / "corpora" / filename).read_text()
        )
        resp = httpx.post(f"{base_url}/corpora/{corpus_id}/load", json=doc, timeout=30)
        print(f"[loaded] {corpus_id}: HTTP {resp.status_code}")
        resp.raise_for_status()
    for corpus_id, filename in NEGATIVE_FILES:
        doc = json.loads(
            (ROOT / "fixtures" / "corpora" / filename).read_text()
        )
        resp = httpx.post(f"{base_url}/corpora/{corpus_id}/load", json=doc, timeout=30)
        print(
            f"[correctly rejected] {corpus_id}: HTTP {resp.status_code} "
            f"{resp.json()['detail']['error_code']}"
        )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default="data/wfst.sqlite3")
    parser.add_argument("--base-url", default=None)
    args = parser.parse_args()
    if args.base_url:
        load_http(args.base_url)
    else:
        Path(args.db).parent.mkdir(parents=True, exist_ok=True)
        load_direct(args.db)


if __name__ == "__main__":
    main()
