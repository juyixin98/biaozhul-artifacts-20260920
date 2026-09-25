"""CLI 与生命周期辅助函数的补充测试。"""

from __future__ import annotations

import io
import json
from contextlib import redirect_stderr
from pathlib import Path

import pytest

from graphs import chain_dag, shared_view_dag
from tensor_planner.cli import main
from tensor_planner.liveness import (
    live_storage_at,
    peak_concurrency,
    storage_intervals,
)

REQUESTS = Path(__file__).resolve().parent.parent / "examples" / "requests"


def test_cli_run_stdout(capsys: pytest.CaptureFixture[str]) -> None:
    rc = main(["run", str(REQUESTS / "01_chain.json")])
    out = capsys.readouterr().out
    assert rc == 0
    report = json.loads(out)
    assert report["passed"]


def test_cli_run_invalid_exit_2(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"inputs": {}, "ops": []}), encoding="utf-8")
    with redirect_stderr(io.StringIO()):
        rc = main(["run", str(bad)])
    assert rc == 2


def test_cli_run_bad_json_exit_2(tmp_path: Path) -> None:
    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    with redirect_stderr(io.StringIO()):
        rc = main(["run", str(bad)])
    assert rc == 2


def test_cli_failed_verification_exit_1(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # 预算小到必然失败：1 字节装不下任何 double 张量
    spec = {
        "inputs": {"A": [2], "B": [2]},
        "ops": [{"name": "C", "op": "add", "inputs": ["A", "B"], "time": 1}],
        "budget_bytes": 1,
    }
    path = tmp_path / "r.json"
    path.write_text(json.dumps(spec), encoding="utf-8")
    rc = main(["run", str(path)])
    assert rc == 1
    report = json.loads(capsys.readouterr().out)
    assert not report["passed"]


def test_cli_stdin(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    spec = (REQUESTS / "01_chain.json").read_text(encoding="utf-8")
    monkeypatch.setattr("sys.stdin", io.StringIO(spec))
    rc = main(["run", "-"])
    assert rc == 0
    assert json.loads(capsys.readouterr().out)["passed"]


def test_live_storage_at() -> None:
    dag = chain_dag()
    stor = storage_intervals(dag)
    # t=0：输入 A、B 存活
    assert live_storage_at(stor, 0) == ["A", "B"]
    # t=1：B、C 存活（A 在 t=1 读完即死）
    assert live_storage_at(stor, 1) == ["B", "C"]
    # t=2：仅输出 D
    assert live_storage_at(stor, 2) == ["D"]
    # 超出 horizon：无存活
    assert live_storage_at(stor, 99) == []


def test_peak_concurrency() -> None:
    stor = storage_intervals(chain_dag())
    peak, t = peak_concurrency(stor)
    assert peak == 2
    assert t in (0, 1, 2)


def test_peak_concurrency_shared_view() -> None:
    stor = storage_intervals(shared_view_dag())
    peak, t = peak_concurrency(stor)
    # t=3：X、P、A 三个存储组并存
    assert peak == 3
    assert t == 3


def test_peak_concurrency_empty() -> None:
    assert peak_concurrency({}) == (0, 0)
