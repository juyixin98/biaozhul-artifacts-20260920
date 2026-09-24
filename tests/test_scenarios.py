"""All shipped scenario examples must satisfy their own expectations."""
from pathlib import Path

import pytest

from time_alignment.cli import cmd_replay
from time_alignment.scenario import evaluate, load_scenario, run_scenario

EXAMPLES = sorted(Path(__file__).resolve().parent.parent.joinpath(
    "examples").glob("*.json"))


@pytest.mark.parametrize("path", EXAMPLES, ids=lambda p: p.name)
def test_scenario_expectations(path):
    ev = evaluate(load_scenario(path))
    assert ev.ok, "failures: " + "; ".join(ev.failures)


def test_cli_replay_with_db_and_verify(tmp_path, monkeypatch):
    scenario = EXAMPLES[0]
    db = tmp_path / "x.db"
    keyf = tmp_path / "k.key"
    keyf.write_bytes(b"a" * 64)
    rc = cmd_replay(type("A", (), {
        "scenario": str(scenario), "db": str(db), "key": str(keyf),
        "export": str(tmp_path / "x.jsonl"), "check_expect": True})())
    assert rc == 0
    assert db.exists()

    from time_alignment.storage import EvidenceStore, load_key
    with EvidenceStore(db, load_key(keyf)) as store:
        assert store.verify().ok


def test_run_scenario_counts():
    m, result = run_scenario(load_scenario(
        Path(__file__).resolve().parent.parent / "examples" / "burst.json"))
    statuses = [p["status"] for p in result.pairs]
    assert statuses.count("matched") == 6
    assert "expired_unpaired" in statuses
    assert m.total_pairs == 6
