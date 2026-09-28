"""cli 演示入口测试：直接调用 main() 并检查退出码。"""

from __future__ import annotations

from gradbucket import cli


def test_cli_demo_all_exits_zero(capsys):
    rc = cli.main(["demo-all"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "总体判断: 通过" in out
    # 输出包含桶世代与归约依据。
    assert "世代迁移: 0 -> 1" in out
    assert "weight_total" in out


def test_cli_single_scenarios_exit_codes(capsys):
    for name, expected_verdict in [
        ("demo-happy", "ACCEPTED"),
        ("demo-unequal", "ACCEPTED"),
        ("demo-omit", "ACCEPTED"),
        ("demo-lost", "REJECTED_WORKER_LOST"),
        ("demo-pending", "INDETERMINATE_PENDING"),
    ]:
        rc = cli.main([name])
        out = capsys.readouterr().out
        assert rc == 0, f"{name} 应返回 0（场景本身是预期判定）"
        assert expected_verdict in out


def test_cli_dumps_diagnostics_jsonl(tmp_path, capsys):
    path = tmp_path / "diag.jsonl"
    rc = cli.main(["demo-unequal", "--dump-diag", str(path)])
    capsys.readouterr()
    assert rc == 0
    lines = path.read_text(encoding="utf-8").strip().splitlines()
    assert lines
    assert all('"verdict"' in line for line in lines)
