"""pytest 公共夹具与诊断钩子。

诊断要求：测试日志能关联输入与运行身份，并显示版本、进度与判定依据。
* 每个测试会话分配一个 run_id，写入 logs/pytest-run-<run_id>.log；
* 会话开始记录相关版本；
* 每个用例开始/结束记录节点 id（参数化用例的参数即输入身份）。
"""

from __future__ import annotations

import logging
import platform
import sys
import uuid
from collections.abc import Iterator
from datetime import datetime, timezone
from pathlib import Path

import fastapi
import pytest

from app import __version__
from app.config import Settings
from app.api.app import create_app

LOG_DIR = Path(__file__).resolve().parent.parent / "logs"


def _stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


@pytest.fixture(scope="session")
def run_id() -> str:
    rid = f"pytest-{_stamp()}-{uuid.uuid4().hex[:6]}"
    log_path = LOG_DIR / f"run-{rid}.log"
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    handler = logging.FileHandler(log_path, encoding="utf-8")
    handler.setFormatter(
        logging.Formatter(
            "%(asctime)s | %(levelname)-7s | run=" + rid + " | %(name)s | %(message)s"
        )
    )
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.handlers.clear()
    root.addHandler(handler)
    stream = logging.StreamHandler()
    stream.setFormatter(logging.Formatter("%(levelname)-7s | %(name)s | %(message)s"))
    root.addHandler(stream)
    logging.info(
        "test session start run_id=%s python=%s platform=%s pytest=%s fastapi=%s app=%s",
        rid,
        platform.python_version(),
        platform.platform(),
        pytest.__version__,
        fastapi.__version__,
        __version__,
    )
    return rid


@pytest.fixture(autouse=True)
def _trace_case(request: pytest.FixtureRequest, run_id: str) -> Iterator[None]:
    logging.info(">>> BEGIN [%s] run_id=%s", request.node.nodeid, run_id)
    yield
    logging.info("<<< END   [%s] outcome=%s", request.node.nodeid, "completed")


@pytest.fixture
def tmp_settings(tmp_path: Path) -> Settings:
    return Settings(
        db_path=tmp_path / "test_index.sqlite3",
        log_dir=tmp_path / "logs",
        log_level="INFO",
        host="127.0.0.1",
        port=0,
        default_index_name="test_default",
    )


@pytest.fixture
def client_factory(tmp_settings: Settings):
    """生成隔离的 TestClient 应用工厂（每个用例独立 SQLite 文件）。"""
    from fastapi.testclient import TestClient

    created: list[TestClient] = []

    def _factory(*, autoload: bool = False) -> TestClient:
        application = create_app(settings=tmp_settings, autoload=autoload)
        test_client = TestClient(application)
        created.append(test_client)
        return test_client

    yield _factory
    for test_client in created:
        test_client.close()
