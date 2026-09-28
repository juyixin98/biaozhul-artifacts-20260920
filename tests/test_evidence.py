"""Evidence store persistence tests."""

from __future__ import annotations

from app.store import EvidenceStore


def _sample_run(**overrides):
    base = dict(
        run_id="run-abc",
        model={"name": "demo", "domains": {"x": [1, 2]},
               "binary_constraints": [], "all_different": []},
        status="sat",
        solution={"x": 1},
        stats={"nodes": 0, "backtracks": 0, "prunes": 0},
        failure=None,
        reasons=[
            {
                "seq": 0,
                "variable": "x",
                "value": 2,
                "constraint": "alldifferent[0]",
                "kind": "alldifferent_matching",
                "detail": {"rule": "example"},
            }
        ],
        solver_version="1.0.0",
    )
    base.update(overrides)
    return base


def test_save_and_get_roundtrip(tmp_path) -> None:
    db = tmp_path / "evidence.db"
    store = EvidenceStore(str(db))
    store.save_run(**_sample_run())

    record = store.get_run("run-abc")
    assert record is not None
    assert record.status == "sat"
    assert record.solution == {"x": 1}
    assert record.model["name"] == "demo"

    reasons = store.get_reasons("run-abc")
    assert len(reasons) == 1
    assert reasons[0]["variable"] == "x"
    assert reasons[0]["detail"] == {"rule": "example"}
    store.close()


def test_data_persists_across_connections(tmp_path) -> None:
    db = tmp_path / "evidence.db"
    store = EvidenceStore(str(db))
    store.save_run(**_sample_run())
    store.close()

    reopened = EvidenceStore(str(db))
    assert reopened.get_run("run-abc") is not None
    assert len(reopened.get_reasons("run-abc")) == 1
    reopened.close()


def test_list_runs_newest_first(tmp_path) -> None:
    store = EvidenceStore(":memory:")
    for index in range(3):
        store.save_run(**_sample_run(run_id=f"run-{index}", model={
            "name": f"demo{index}", "domains": {"x": [1]},
            "binary_constraints": [], "all_different": []}))
    runs = store.list_runs()
    assert len(runs) == 3
    assert {row["run_id"] for row in runs} == {"run-0", "run-1", "run-2"}
    assert all("model" not in row for row in runs)


def test_unknown_run_returns_none(tmp_path) -> None:
    store = EvidenceStore(":memory:")
    assert store.get_run("missing") is None
    assert store.get_reasons("missing") == []
