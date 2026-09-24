"""pytest 公共夹具：每个测试用独立临时 SQLite + 自动 HMAC 签名的测试客户端。"""
from __future__ import annotations

import json
import os
import tempfile

import pytest
from fastapi.testclient import TestClient

# 必须在导入 app.config 之前指定独立测试数据库
_tmpdir = tempfile.mkdtemp(prefix="dispatch-test-")
os.environ["DISPATCH_DB_PATH"] = os.path.join(_tmpdir, "test.db")

from app import config, crypto, database  # noqa: E402
from app.main import app  # noqa: E402


@pytest.fixture(autouse=True)
def fresh_db():
    path = config.DB_PATH
    for suffix in ("", "-wal", "-shm"):
        p = path + suffix
        if os.path.exists(p):
            os.remove(p)
    database.init_db(path)
    # 清空内存 nonce 表（fresh_db 已重建文件，这里仅防御）
    yield
    conn = database.connect(path)
    try:
        conn.execute("DELETE FROM used_nonces")
    finally:
        conn.close()


@pytest.fixture
def client():
    with TestClient(app) as c:
        yield c


@pytest.fixture
def api(client):
    """自动给 /api 请求计算真实 HMAC 签名的小客户端。"""

    class SignedApi:
        def _send(self, method, path, obj=None, *, sign=True,
                  extra_headers=None, raw_body: bytes | None = None):
            if raw_body is not None:
                body = raw_body
            elif obj is not None:
                body = json.dumps(obj, separators=(",", ":")).encode()
            else:
                body = b""
            headers = {"content-type": "application/json"}
            if sign:
                headers.update(crypto.sign_request(method, path, body))
            if extra_headers:
                # 显式头优先（用于构造错误签名/过期时间戳/重放等用例）
                headers.update(extra_headers)
            return client.request(method, path, content=body, headers=headers)

        def post(self, path, obj=None, **kw):
            return self._send("POST", path, obj, **kw)

        def get(self, path, obj=None, **kw):
            return self._send("GET", path, obj, **kw)

        def robot(self, rid, x, y, battery):
            r = self.post("/api/robots",
                          {"id": rid, "x": x, "y": y, "battery_wh": battery})
            assert r.status_code == 200, r.text
            return r.json()

        def charger(self, cid, x, y):
            r = self.post("/api/chargers", {"id": cid, "x": x, "y": y})
            assert r.status_code == 200, r.text
            return r.json()

        def task(self, tid, x, y, payload, wait=0.0):
            r = self.post("/api/tasks",
                          {"id": tid, "x": x, "y": y,
                           "payload_kg": payload, "wait_s": wait})
            assert r.status_code == 200, r.text
            return r.json()

        def dispatch(self, task_ids, margin=None):
            payload = {"task_ids": task_ids}
            if margin is not None:
                payload["options"] = {"safety_margin_wh": margin}
            return self.post("/api/dispatch", payload)

    return SignedApi()
