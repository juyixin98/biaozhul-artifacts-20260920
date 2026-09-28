"""运行日志与幂等 run_id 状态冲突。"""

from __future__ import annotations

import json

import pytest

from polyroots import solve_polynomial
from polyroots.errors import ConflictingStateError
from polyroots.runlog import RunStore
from tests.conftest import poly_from_roots

pytestmark = pytest.mark.integration


@pytest.fixture
def store(tmp_path) -> RunStore:
    return RunStore(str(tmp_path / "runs"))


def _payload(coeffs):
    return [[c.real, c.imag] for c in coeffs[::-1]]


def test_run_is_persisted_and_replayable(store) -> None:
    coeffs = poly_from_roots([1 + 1j, 2 - 2j, -3 + 0j])
    res = solve_polynomial(_payload(coeffs), store=store,
                           run_id="replay-001")
    loaded = store.load_run("replay-001")
    assert loaded is not None
    assert loaded["run_id"] == "replay-001"
    assert loaded["status"] == res.status.value
    assert len(loaded["roots"]) == 3
    # 关键中间状态与判断理由被保留，可据此重放
    assert "intermediate" in loaded["kernel"]
    assert all("note" in r for r in loaded["roots"])
    # 文件是合法 JSON
    path = store.write_run(loaded)
    json.loads(path.read_text(encoding="utf-8"))


def test_same_run_id_same_input_is_idempotent(store) -> None:
    coeffs = poly_from_roots([1.0, 2.0, 3.0])
    solve_polynomial(_payload(coeffs), store=store, run_id="idem")
    # 完全相同输入重放：允许
    res2 = solve_polynomial(_payload(coeffs), store=store, run_id="idem")
    assert res2.run_id == "idem"


def test_same_run_id_different_input_is_state_conflict(store) -> None:
    c1 = poly_from_roots([1.0, 2.0, 3.0])
    c2 = poly_from_roots([4.0, 5.0, 6.0])
    solve_polynomial(_payload(c1), store=store, run_id="clash")
    with pytest.raises(ConflictingStateError) as exc:
        solve_polynomial(_payload(c2), store=store, run_id="clash")
    assert exc.value.category == "state_conflict"
    assert exc.value.http_status == 409


def test_load_missing_run_returns_none(store) -> None:
    assert store.load_run("does-not-exist") is None
