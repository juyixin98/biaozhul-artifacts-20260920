"""状态持久化测试：重启后信任根与登记记录保留。"""
from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from app.main import create_app
from scripts.sighelp import (
    approval_block,
    b64e,
    make_envelope,
    make_root_body,
)


def test_state_survives_restart(tmp_path, parties):
    state_file = tmp_path / "state.json"
    app = create_app(persist_path=str(state_file))
    with TestClient(app) as c:
        body = make_root_body(1, 2, 2, parties["root_pub"], parties["art_pub"])
        assert c.post("/roots/bootstrap", json=body.model_dump()).status_code == 201

        content = b"persist-me"
        env = make_envelope(content, "report", "1.0.0", parties["art_priv"][:2])
        assert c.post(
            "/artifacts/register",
            json={"envelope": env.model_dump(), "content_base64": b64e(content)},
        ).status_code == 201

    # 落盘文件存在，且不含任何「PRIVATE KEY」字样
    raw = Path(state_file).read_text(encoding="utf-8")
    assert "PRIVATE KEY" not in raw

    # 重启：新 app 实例从同一文件加载
    app2 = create_app(persist_path=str(state_file))
    with TestClient(app2) as c2:
        assert c2.get("/roots/current").json()["version"] == 1
        r = c2.post(
            "/verify",
            json={"envelope": env.model_dump(), "content_base64": b64e(content)},
        )
        assert r.json()["accepted"] is True
        # 防重复签名状态也保留
        r2 = c2.post(
            "/artifacts/register",
            json={"envelope": env.model_dump(), "content_base64": b64e(content)},
        )
        assert r2.status_code == 409

    # 轮换后再重启，版本保持为 2
    v2 = make_root_body(
        2, 2, 2, parties["new_root_pub"], parties["new_art_pub"]
    )
    approvals = [approval_block(p, v2) for p in parties["root_priv"][:2]]
    app2b = create_app(persist_path=str(state_file))
    with TestClient(app2b) as c2b:
        assert c2b.post(
            "/roots/rotate",
            json={"new_root": v2.model_dump(), "approvals": [a.model_dump() for a in approvals]},
        ).status_code == 200

    app3 = create_app(persist_path=str(state_file))
    with TestClient(app3) as c3:
        assert c3.get("/roots/current").json()["version"] == 2
