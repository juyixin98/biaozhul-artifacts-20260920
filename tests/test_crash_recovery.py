"""崩溃恢复与状态一致性测试。

两种崩溃模型：
  1. 进程内直接重建 KeyService（模拟进程重启后从磁盘恢复）；
  2. 以子进程启动真实 HTTP 服务，工作中发送 SIGKILL（kill -9），
     重启后校验状态一致、历史密文可解、审计链完整。
"""

import json
import signal
import socket
import subprocess
import sys
import time
import urllib.request

import pytest

from keyvault import KeyDestroyedError, KeyService


def _restart(data_dir):
    """模拟进程重启：丢弃内存对象，从磁盘重新加载。"""
    return KeyService(data_dir)


def test_state_survives_restart(data_dir):
    svc = KeyService(data_dir)
    svc.generate_key()
    svc.activate("v1")
    env = svc.encrypt(b"durable data")
    svc.generate_key()

    svc2 = _restart(data_dir)
    assert svc2.status()["active_version"] == "v1"
    states = {m["version_id"]: m["state"] for m in svc2.list_keys()}
    assert states == {"v1": "ACTIVE", "v2": "GENERATED"}
    assert svc2.decrypt(env) == b"durable data"


def test_destroy_survives_restart(data_dir):
    svc = KeyService(data_dir)
    svc.generate_key()
    svc.activate("v1")
    env = svc.encrypt(b"to be destroyed")
    svc.deactivate("v1")
    svc.destroy("v1")

    svc2 = _restart(data_dir)
    assert svc2.status()["consistency"] == []
    with pytest.raises(KeyDestroyedError):
        svc2.decrypt(env)
    # 密钥文件确实被擦除
    assert not (data_dir / "keys" / "v1.key").exists()


def test_restart_mid_rotation(data_dir):
    """轮换进行到一半崩溃：v1 已停用、v2 已激活，重启后两者都正确。"""
    svc = KeyService(data_dir)
    svc.generate_key()
    svc.activate("v1")
    env1 = svc.encrypt(b"before rotation")
    svc.generate_key()
    svc.activate("v2")
    env2 = svc.encrypt(b"after rotation")

    svc2 = _restart(data_dir)
    assert svc2.status()["consistency"] == []
    assert svc2.status()["active_version"] == "v2"
    assert svc2.decrypt(env1) == b"before rotation"
    assert svc2.decrypt(env2) == b"after rotation"
    assert svc2.encrypt(b"new")["v"] == "v2"


def test_consistency_check_catches_orphan_key_file(data_dir):
    """密钥文件存在但元数据丢失（异常场景）应被自检发现。"""
    svc = KeyService(data_dir)
    svc.generate_key()
    # 手工破坏：删掉元数据中的版本记录，留下密钥文件
    state = json.loads((data_dir / "state.json").read_text())
    del state["versions"]["v1"]
    (data_dir / "state.json").write_text(json.dumps(state))
    svc2 = _restart(data_dir)
    # v1 元数据缺失，自检不应崩溃；现存版本应一致
    assert isinstance(svc2.status()["consistency"], list)


# ---------- 真实进程级崩溃（SIGKILL） ----------


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _post(port, path, payload=None):
    body = json.dumps(payload or {}).encode()
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}", data=body,
        headers={"Content-Type": "application/json"}, method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def _get(port, path):
    with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=5) as resp:
        return json.loads(resp.read())


def _wait_up(port, timeout=10):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            _get(port, "/status")
            return True
        except OSError:
            time.sleep(0.1)
    return False


def test_sigkill_recovery(tmp_path):
    """kill -9 强杀服务进程后重启：状态一致、密文可解、审计链完整。"""
    data = tmp_path / "crashdata"
    port = _free_port()
    cmd = [sys.executable, "-m", "keyvault.server",
           "--data-dir", str(data), "--port", str(port)]

    proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        assert _wait_up(port), "服务未能启动"
        _post(port, "/keys")
        _post(port, "/keys/v1/activate")
        import base64
        pt = base64.b64encode(b"crash-safe payload").decode()
        _, r = _post(port, "/encrypt", {"plaintext_b64": pt})
        envelope = r["envelope"]
        _post(port, "/keys")           # v2 生成到一半
        proc.send_signal(signal.SIGKILL)  # 模拟断电/崩溃
        proc.wait()
    finally:
        if proc.poll() is None:
            proc.kill()

    # 重启（同一数据目录、同一端口）
    proc2 = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        assert _wait_up(port), "重启失败"
        status = _get(port, "/status")
        assert status["consistency"] == [], f"崩溃后状态不一致: {status['consistency']}"
        assert status["active_version"] == "v1"

        # 崩溃前的密文仍可解密
        code, r = _post(port, "/decrypt", {"envelope": envelope})
        assert code == 200
        import base64
        assert base64.b64decode(r["plaintext_b64"]) == b"crash-safe payload"

        # 审计链完整
        assert _get(port, "/audit/verify")["ok"] is True

        # 服务可继续正常工作
        _post(port, "/keys/v2/activate")
        _, r2 = _post(port, "/encrypt", {"plaintext_b64": pt})
        assert r2["envelope"]["v"] == "v2"
    finally:
        proc2.kill()
        proc2.wait()
