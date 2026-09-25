"""HTTP 服务端到端：训练 → 中断 → 通过 API 恢复 → 结果一致。"""

import json
import threading
import urllib.request
from http.server import ThreadingHTTPServer

import numpy as np
import pytest

from checkpoint_resume import TrainConfig, Trainer
from checkpoint_resume.server import _State, make_handler


@pytest.fixture()
def server(tmp_path):
    state = _State()
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(state))
    port = httpd.server_address[1]
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{port}", tmp_path
    httpd.shutdown()
    httpd.server_close()
    thread.join(timeout=5)


def _post(url, path, payload):
    req = urllib.request.Request(
        url + path,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _get(url, path):
    with urllib.request.urlopen(url + path, timeout=10) as resp:
        return json.loads(resp.read().decode("utf-8"))


def test_health(server):
    url, _ = server
    assert _get(url, "/health")["ok"] is True


def test_train_pause_resume_via_api(server):
    url, tmp_path = server
    ckpt_dir = str(tmp_path / "svc_ckpts")
    config = {"checkpoint_dir": ckpt_dir, "checkpoint_every": 10**9}

    r1 = _post(url, "/train", {"config": config, "steps": 10, "resume": False})
    assert r1["ok"] and r1["steps_run"] == 10
    assert r1["status"]["global_step"] == 10

    # 模拟服务重启：新请求、新 Trainer 实例，从检查点恢复
    r2 = _post(url, "/train", {"config": config, "steps": 15, "resume": True})
    assert r2["ok"] and r2["resumed_from"] is not None
    assert r2["status"]["global_step"] == 25

    # 与不中断基准对比
    ref = Trainer(
        TrainConfig(checkpoint_dir=str(tmp_path / "ref"), checkpoint_every=10**9),
        resume=False,
    )
    ref.train(25)
    assert r2["status"]["full_loss"] == ref.full_loss()

    listing = _get(url, "/checkpoints")
    assert listing["ok"]
    assert all(item["valid"] for item in listing["checkpoints"])

    ckpt_path = listing["checkpoints"][-1]["path"]
    assert _post(url, "/verify", {"path": ckpt_path})["valid"] is True


def test_verify_detects_corruption(server):
    url, tmp_path = server
    ckpt_dir = str(tmp_path / "svc_ckpts")
    _post(url, "/train", {"config": {"checkpoint_dir": ckpt_dir}, "steps": 5})

    listing = _get(url, "/checkpoints")
    ckpt_path = listing["checkpoints"][-1]["path"]
    data = bytearray(open(ckpt_path, "rb").read())
    data[50] ^= 0xFF
    open(ckpt_path, "wb").write(bytes(data))

    result = _post(url, "/verify", {"path": ckpt_path})
    assert result["valid"] is False
    assert "SHA-256" in result["error"]


def test_bad_request_rejected(server):
    url, _ = server
    req = urllib.request.Request(
        url + "/train",
        data=b"not json",
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with pytest.raises(urllib.error.HTTPError) as exc_info:
        urllib.request.urlopen(req, timeout=10)
    assert exc_info.value.code == 400
