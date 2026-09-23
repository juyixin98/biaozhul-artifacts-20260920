# -*- coding: utf-8 -*-
"""pytest 公共夹具：临时 SQLite + FastAPI TestClient + 引擎/中继器。"""
from __future__ import annotations

import os
import tempfile

import pytest
from fastapi.testclient import TestClient

from app.api import create_app
from app.engine import Engine
from app.relayer import Relayer
from app.storage import Database


@pytest.fixture()
def tmp_db(tmp_path):
    return str(tmp_path / "test.db")


@pytest.fixture()
def engine(tmp_db):
    return Engine(Database(tmp_db))


@pytest.fixture()
def relay(engine):
    return Relayer(engine)


@pytest.fixture()
def client(tmp_db):
    app = create_app(Database(tmp_db))
    with TestClient(app) as c:
        yield c


@pytest.fixture()
def network(relay):
    """两条已建客户端/连接的链。"""
    relay.setup_two_chains("chainA", "chainB", genesis_time_nanos=1_000_000_000_000)
    return relay
