"""pytest 公共夹具：隔离的临时 DB / 日志目录、已摄取模型、运行身份。"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

FIXTURES = ROOT / "fixtures" / "corpora"


@pytest.fixture()
def settings(tmp_path):
    from wfst.config import Settings

    return Settings(
        db_path=tmp_path / "test.db",
        default_k=5,
        default_budget=200_000,
        max_input_len=64,
        smoothing_count=0.5,
        log_dir=tmp_path / "logs",
    )


@pytest.fixture()
def manager(settings):
    from wfst.index.store import Store
    from wfst.manager import ModelManager

    store = Store(settings.db_path)
    mgr = ModelManager(store, settings)
    yield mgr
    store.close()


@pytest.fixture()
def char_model(manager):
    return manager.ingest_file(FIXTURES / "char_morph_demo.json").model


@pytest.fixture()
def eps_model(manager):
    return manager.ingest_file(FIXTURES / "epsilon_ambiguity_demo.json").model


@pytest.fixture()
def word_model(manager):
    return manager.ingest_file(FIXTURES / "word_morph_demo.json").model


@pytest.fixture()
def client(settings, manager):
    from fastapi.testclient import TestClient
    from wfst.service.app import create_app

    app = create_app(settings, manager)
    # 摄取两个字符级夹具。
    manager.ingest_file(FIXTURES / "char_morph_demo.json")
    manager.ingest_file(FIXTURES / "epsilon_ambiguity_demo.json")
    with TestClient(app) as c:
        yield c
