"""Diagnostics: journals are replayable and carry state + rationale."""

from __future__ import annotations

import json
from pathlib import Path

from entity_resolution.models import CorpusIn, LinkIn
from entity_resolution.service import EntityResolutionService


def _load_chain(service: EntityResolutionService) -> None:
    service.load_corpus(
        CorpusIn(
            records=[
                {"id": "A", "name": "Pioneer Corp"},
                {"id": "B", "name": "Pioneer Corporation"},
                {"id": "C", "name": "Pioneer Foods"},
            ],
            cannot_links=[("A", "C")],
        )
    )


def _read_journal(log_dir: Path, run_id: str) -> dict:
    path = log_dir / f"{run_id}.jsonl"
    lines = path.read_text(encoding="utf-8").strip().splitlines()
    # One terminal JSON object per call (file accumulates across calls only by
    # run id); parse the last line belonging to this run.
    return json.loads(lines[-1])


def test_successful_run_journal_records_stages_and_decisions(
    service: EntityResolutionService, paths: Path
) -> None:
    _load_chain(service)
    solution = service.resolve(0.45)
    record = _read_journal(paths / "logs", solution.run_id)

    assert record["status"] == "ok"
    assert record["run_id"] == solution.run_id
    stage_names = [s["stage"] for s in record["stages"]]
    assert "candidates" in stage_names
    assert "solved" in stage_names

    # Intermediate candidate state is present with per-pair scores.
    candidates_stage = next(s for s in record["stages"] if s["stage"] == "candidates")
    assert "A|B" in candidates_stage["state"]["pairs"]

    # Rationale distinguishes a cannot-link rejection from a soft candidate.
    subjects = {d["subject"]: d["rationale"] for d in record["decisions"]}
    assert any("A~C" in s for s in subjects)


def test_failed_run_journal_records_error_category(
    service: EntityResolutionService, paths: Path
) -> None:
    service.load_corpus(
        CorpusIn(
            records=[{"id": "A", "name": "X"}, {"id": "B", "name": "Y"}],
            must_links=[("A", "B")],
        )
    )
    try:
        service.add_link(LinkIn(left="A", right="B", kind="cannot"))
        raised = False
    except Exception:
        raised = True
    assert raised

    log_dir = paths / "logs"
    journals = sorted(log_dir.glob("link-*.jsonl"))
    assert journals
    record = json.loads(journals[-1].read_text(encoding="utf-8").strip().splitlines()[-1])
    assert record["status"] == "error"
    assert record["error"]["category"] == "STATE_CONFLICT"
    assert record["error"]["details"]["reason"] == "direct_contradiction"


def test_run_ids_are_unique_and_sortable(
    service: EntityResolutionService, paths: Path
) -> None:
    _load_chain(service)
    ids = [service.resolve(0.45).run_id for _ in range(3)]
    assert len(set(ids)) == 3
    # Timestamp prefix makes them lexicographically time-ordered.
    assert ids == sorted(ids)
