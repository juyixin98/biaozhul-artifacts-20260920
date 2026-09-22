"""共享夹具：每个测试用独立临时 SQLite 文件，跑迁移、载内置规则。"""
from __future__ import annotations

import os
import pathlib
import tempfile
import time

import pytest

# 必须在导入 app.* 之前设置
_TMP = tempfile.mkdtemp(prefix="kex-test-")
_DB_PATH = os.path.join(_TMP, "test.db")
os.environ["KEX_DB_URL"] = f"sqlite:////{_DB_PATH}"
os.environ["KEX_ADMIN_KEY"] = "test-admin-key"
os.environ["KEX_ENABLE_WORKER_THREADS"] = "0"
os.environ["KEX_LEASE_SECONDS"] = "30"
os.environ["KEX_MAX_WORKERS"] = "2"

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
_RULES_DIR = REPO_ROOT / "rules"
_MIGRATIONS_DIR = REPO_ROOT / "migrations"
os.environ["KEX_BUILTIN_RULES_DIR"] = str(_RULES_DIR)
os.environ["KEX_MIGRATIONS_DIR"] = str(_MIGRATIONS_DIR)

from app import db  # noqa: E402
from app.migrations import run_migrations  # noqa: E402
from app.services import rule_packs as packs  # noqa: E402


def _clear_business_tables(conn):
    conn.execute("PRAGMA foreign_keys=OFF")
    for tbl in (
        "postings", "doc_term_stats", "index_df", "mentions", "entities",
        "jobs", "checkpoints", "worker_registry", "rule_activations",
        "documents", "index_generations", "workspaces", "blobs",
    ):
        conn.execute(f"DELETE FROM {tbl}")
    conn.execute("PRAGMA foreign_keys=ON")


@pytest.fixture(scope="session", autouse=True)
def _prepare_database():
    run_migrations()
    packs.load_builtin_packs()
    yield


@pytest.fixture
def fresh_db():
    """每个测试清空业务表（保留已发布规则包与迁移记录）。"""
    import sqlite3

    conn = sqlite3.connect(_DB_PATH, timeout=30)
    try:
        _clear_business_tables(conn)
        conn.commit()
    finally:
        conn.close()
    packs._compiled_cache.clear()
    db.reset_engine()
    yield _DB_PATH
    db.reset_engine()


@pytest.fixture
def app(fresh_db):
    from app.app import create_app

    application = create_app(start_workers=False)
    application.testing = True
    return application


@pytest.fixture
def client(app):
    return app.test_client()


@pytest.fixture
def admin_headers():
    return {"X-Admin-Key": "test-admin-key"}


@pytest.fixture
def workspace_factory(client):
    """创建工作区，返回工厂 (name) -> {id, key, headers}。"""
    created = []

    def _make(name: str = "测试工作区"):
        resp = client.post("/api/workspaces", json={"name": name})
        assert resp.status_code == 201, resp.data
        data = resp.get_json()
        info = {
            "id": data["id"],
            "key": data["api_key"],
            "headers": {"X-Workspace-Key": data["api_key"]},
            "default_rule": data["active_rule_pack_version"],
        }
        created.append(info)
        return info

    return _make


@pytest.fixture
def ws(workspace_factory):
    return workspace_factory()


def _drain_jobs(timeout: float = 10.0) -> dict[str, int]:
    """同步跑完所有待处理作业（不起线程，直接在测试线程执行），返回统计。"""
    from app.services import jobs as jobs_service
    from app.services.worker import Worker

    worker = Worker(worker_id="test-worker", poll_interval=0)
    stats = {"extract": 0, "rebuild": 0, "other": 0}
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            job = jobs_service.claim_job("test-worker")
        except Exception:
            raise
        if job is None:
            break
        if job["kind"] in stats:
            stats[job["kind"]] += 1
        else:
            stats["other"] += 1
        worker._run_one(job)
    return stats


@pytest.fixture
def drain():
    return _drain_jobs


SAMPLE_TEXT_A = """2024年3月15日，北京大学的李明教授在采访中说，团队与阿里巴巴集团
合作，把基于 Python 3.12.1 和 SQLAlchemy 的原型迁移到了 PostgreSQL 16.2。
王芳指出，旧系统使用 SQLite 与 Flask 构建，部署在 Docker 中。
Alan Kay 在 Xerox PARC 回忆 Smalltalk；Linus Torvalds 在 Sept 2nd, 2024 讨论调度。
"""

SAMPLE_TEXT_B = """张伟博士今天表示，华为技术有限公司将在 2025-01-08 发布新工具。
该工具用 Rust 重写了 build_index 模块，兼容 PostgreSQL，也支持 SQLite 数据库。
明天，王经理会在清华大学介绍 openAi 兼容层。
"""

SAMPLE_TEXT_C = """面向对象编程与动态规划是两门经典课程。
Google 和 Microsoft 的工程师常使用 Python 与 Docker。
Donald Knuth 教授撰写的书出版于 2024年6月。
"""
