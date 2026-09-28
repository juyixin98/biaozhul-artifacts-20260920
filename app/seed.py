"""Seed a demo database with deterministic synthetic corpus.

Usage::

    BRACKET_INDEX_DB=/tmp/demo.db python -m app.seed

Creates one balanced multi-chunk document and one document deliberately
containing crossing/unbalanced brackets, then prints the verify verdicts.
All data is local and synthetic.
"""
from __future__ import annotations

from .config import Settings
from .corpus import default_lexicon, synthetic_document
from .service import DocumentService
from .storage import Database


def main() -> None:
    settings = Settings.from_env()
    db = Database(settings.db_path)
    service = DocumentService(
        db, lexicon=default_lexicon(), chunk_size=settings.chunk_size
    )

    balanced_text = synthetic_document(seed=1, target_length=4096)
    doc = service.create_document("synthetic_balanced", balanced_text)
    print(f"created id={doc.id} name=synthetic_balanced "
          f"len={doc.length} version={doc.version}")
    verdict = service.verify_against_full_scan(doc.id)
    print(f"verify agrees={verdict['agrees']} balanced={verdict['balanced']}")

    defective = "x = (a + [b) * c]; y = {" + "nested([deep])" * 4
    doc2 = service.create_document("handcrafted_defects", defective)
    report = service.shortest_unbalanced(doc2.id)
    print(f"created id={doc2.id} name=handcrafted_defects len={doc2.length}")
    print(f"balanced={report['balanced']} shortest={report['defect']}")

    db.close()


if __name__ == "__main__":
    main()
