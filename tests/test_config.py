"""配置层测试：环境变量解析、非法值快速失败。"""

from __future__ import annotations

import pytest

from wfst.config import Settings


def test_defaults_when_env_absent(monkeypatch, tmp_path):
    for var in ["WFST_DB_PATH", "WFST_DEFAULT_K", "WFST_DEFAULT_BUDGET",
                "WFST_MAX_INPUT_LEN", "WFST_SMOOTHING_COUNT", "WFST_LOG_DIR"]:
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("WFST_DB_PATH", str(tmp_path / "x.db"))
    monkeypatch.setenv("WFST_LOG_DIR", str(tmp_path / "logs"))
    s = Settings.from_env()
    assert s.default_k == 5
    assert s.default_budget == 200_000
    assert s.max_input_len == 64
    assert s.smoothing_count == 0.5


def test_env_overrides(monkeypatch, tmp_path):
    monkeypatch.setenv("WFST_DB_PATH", str(tmp_path / "y.db"))
    monkeypatch.setenv("WFST_LOG_DIR", str(tmp_path / "l"))
    monkeypatch.setenv("WFST_DEFAULT_K", "3")
    monkeypatch.setenv("WFST_DEFAULT_BUDGET", "999")
    monkeypatch.setenv("WFST_MAX_INPUT_LEN", "12")
    monkeypatch.setenv("WFST_SMOOTHING_COUNT", "0.1")
    s = Settings.from_env()
    assert (s.default_k, s.default_budget, s.max_input_len) == (3, 999, 12)
    assert s.smoothing_count == pytest.approx(0.1)


@pytest.mark.parametrize("var,bad", [
    ("WFST_DEFAULT_K", "0"),
    ("WFST_DEFAULT_BUDGET", "-1"),
    ("WFST_MAX_INPUT_LEN", "0"),
    ("WFST_SMOOTHING_COUNT", "-0.2"),
])
def test_invalid_env_fails_fast(monkeypatch, tmp_path, var, bad):
    monkeypatch.setenv("WFST_DB_PATH", str(tmp_path / "z.db"))
    monkeypatch.setenv("WFST_LOG_DIR", str(tmp_path / "l"))
    monkeypatch.setenv(var, bad)
    with pytest.raises(ValueError):
        Settings.from_env()


def test_non_integer_env_fails(monkeypatch, tmp_path):
    monkeypatch.setenv("WFST_DB_PATH", str(tmp_path / "z.db"))
    monkeypatch.setenv("WFST_LOG_DIR", str(tmp_path / "l"))
    monkeypatch.setenv("WFST_DEFAULT_K", "abc")
    with pytest.raises(ValueError):
        Settings.from_env()
