"""服务编排层：诊断日志可关联输入与运行身份，并显示版本与计算步骤。"""

import logging
import platform

import pytest

from minidfa import __version__
from minidfa.corpus import InputMode


@pytest.fixture()
def captured(caplog):
    """minidfa logger 不向 root 传播，测试里显式挂接 caplog 处理器。"""
    logger = logging.getLogger("minidfa")
    logger.addHandler(caplog.handler)
    with caplog.at_level(logging.INFO, logger="minidfa"):
        yield caplog


def test_build_logs_run_identity_and_steps(service, captured):
    report = service.build("diag", ["ape", "apple", "band"])
    text = captured.text

    # 运行身份与输入指纹贯穿全部步骤
    assert f"run={report.run_id}" in text
    assert text.count(report.fingerprint) >= 1
    # 版本信息
    assert f"minidfa={__version__}" in text
    assert platform.python_version() in text
    # 关键计算步骤与判定依据（状态数/边数）
    assert "corpus validated" in text
    assert f"minimized states={report.state_count}" in text
    assert "persisted" in text


def test_failed_build_raises_and_does_not_report_success(service, captured):
    with pytest.raises(Exception) as exc_info:
        service.build("bad", ["b", "a"])  # strict 默认拒绝乱序
    assert exc_info.value.category == "UNSORTED_INPUT"
    # 失败运行不得留下持久化产物
    assert not service.store.exists("bad")


def test_normalize_mode_builds_from_unsorted(service):
    report = service.build("norm", ["b", "a", "b"], InputMode.NORMALIZE)
    assert report.word_count == 2
    assert service.contains("norm", "a")
    assert not service.contains("norm", "c")


def test_stats_and_queries(service):
    service.build("q", ["cat", "cats", "dog"])
    stats = service.stats("q")
    assert stats["word_count"] == 3
    assert stats["minidfa_version"] == __version__
    assert service.prefix_count("q", "cat") == 2
    assert service.prefix_count("q", "do") == 1
    assert service.prefix_count("q", "x") == 0
