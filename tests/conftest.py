"""测试共享夹具与辅助函数。"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.config import Limits, Settings
from app.kernel.lexer import CompiledLexer, Rule
from app.main import create_app
from app.runlog import RunLogger


@pytest.fixture()
def limits() -> Limits:
    return Limits()


@pytest.fixture()
def logger() -> RunLogger:
    return RunLogger("run-test-fixture")


@pytest.fixture()
def client(tmp_path):
    app = create_app(Settings(db_path=str(tmp_path / "test.db"), limits=Limits()))
    with TestClient(app) as c:
        yield c


def make_rules(spec: list[tuple[str, str]] | list[tuple[str, str, int]]) -> list[Rule]:
    """(name, pattern) 或 (name, pattern, priority) 简写 -> Rule 列表。"""
    rules = []
    for i, item in enumerate(spec):
        name, pattern = item[0], item[1]
        priority = item[2] if len(item) > 2 else 0
        rules.append(Rule(index=i, name=name, pattern=pattern, priority=priority, skip=False))
    return rules


def build_lexer(spec, limits: Limits, logger: RunLogger) -> CompiledLexer:
    return CompiledLexer.build(make_rules(spec), limits, logger)


def iter_strings(alphabet: str, max_len: int):
    """按长度递增枚举字母表上的所有串（独立预言机用）。"""
    yield ""
    for length in range(1, max_len + 1):
        for tup in __import__("itertools").product(alphabet, repeat=length):
            yield "".join(tup)
