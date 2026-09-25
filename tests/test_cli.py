"""CLI 端到端：keygen -> policy publish -> export -> verify（含加密）。"""

import json
import os
import subprocess
import sys

import pytest

from mde.canonical import stable_json_dumps

_SRC = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src"))
_ENV = {**os.environ, "PYTHONPATH": _SRC + os.pathsep +
       os.environ.get("PYTHONPATH", "")}


def _run(env_cwd, *args, expect=0):
    proc = subprocess.run(
        [sys.executable, "-m", "mde.cli", *args],
        cwd=env_cwd, capture_output=True, text=True, env=_ENV)
    if proc.returncode != expect:
        raise AssertionError(
            f"command failed ({proc.returncode}): {' '.join(args)}\n"
            f"STDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}")
    out = proc.stdout.strip()
    return json.loads(out) if out else {}


@pytest.fixture
def workspace(tmp_path):
    return str(tmp_path)


POLICY_DOC = {
    "rules": [
        {"path": "name", "action": "allow"},
        {"path": "email", "action": "generalize", "generalizer": "email_mask"},
        {"path": "ssn", "action": "deny"},
        {"path": "age", "action": "generalize", "generalizer": "numeric_bucket",
         "params": {"bins": [0, 18, 65]}},
    ],
    "aliases": [{"canonical": "email", "aliases": ["mail"]}],
}

DATA = {
    "name": "Bob Chen",
    "mail": "bob@example.com",
    "ssn": "999-00-0000",
    "age": 42,
    "surprise_field": "x",
}


def test_cli_full_flow(workspace):
    store_dir = f"{workspace}/policies"
    bundle_dir = f"{workspace}/bundles"
    key_dir = f"{workspace}/keys"
    import os
    os.makedirs(store_dir, exist_ok=True)
    os.makedirs(bundle_dir, exist_ok=True)

    policy_file = f"{workspace}/policy.json"
    data_file = f"{workspace}/data.json"
    bundle_file = f"{workspace}/out.json"
    with open(policy_file, "w") as f:
        json.dump(POLICY_DOC, f)
    with open(data_file, "w") as f:
        json.dump(DATA, f)

    # keygen
    kg = _run(workspace, "keygen", "--dir", key_dir, "--with-fernet")
    assert kg["verifying_key_hex"]
    assert os.path.exists(f"{key_dir}/signing_key.pem")
    # 私钥权限 0600。
    mode = os.stat(f"{key_dir}/signing_key.pem").st_mode & 0o777
    assert mode == 0o600

    # publish
    pub = _run(workspace, "policy", "publish", "--store-dir", store_dir,
               "--id", "hr", "--file", policy_file)
    assert pub["version"] == 1

    # export (明文，固定签名密钥)
    _run(workspace, "export", "--store-dir", store_dir,
         "--data", data_file, "--policy-id", "hr",
         "--purpose", "analytics",
         "--signing-key", f"{key_dir}/signing_key.pem",
         "--bundle-dir", bundle_dir, "--out", bundle_file)
    with open(bundle_file) as f:
        bundle = json.load(f)
    assert bundle["output"]["name"] == "Bob Chen"
    assert bundle["output"]["mail"] == "b***@example.com"
    assert "ssn" not in bundle["output"]
    assert "surprise_field" not in bundle["output"]
    assert bundle["manifest"]["policy"]["version"] == 1

    # verify（提供原始数据，全量重算）
    report = _run(workspace, "verify", "--store-dir", store_dir,
                  "--bundle", bundle_file,
                  "--source-data", data_file)
    assert report["ok"] is True
    assert len(report["checks"]) == 7

    # 加密导出 + 正确密钥核验
    enc_bundle = f"{workspace}/out.enc.json"
    proc = subprocess.run(
        [sys.executable, "-m", "mde.cli", "export",
         "--store-dir", store_dir, "--data", data_file,
         "--policy-id", "hr", "--purpose", "analytics",
         "--signing-key", f"{key_dir}/signing_key.pem",
         "--encrypt", "--encryption-key", f"{key_dir}/fernet.key",
         "--out", enc_bundle],
        cwd=workspace, capture_output=True, text=True, env=_ENV)
    assert proc.returncode == 0, proc.stderr
    rep2 = _run(workspace, "verify", "--store-dir", store_dir,
                "--bundle", enc_bundle,
                "--encryption-key", f"{key_dir}/fernet.key",
                "--source-data", data_file)
    assert rep2["ok"] is True


def test_cli_verify_detects_tampering(workspace, tmp_path):
    import os
    store_dir = f"{workspace}/policies"
    os.makedirs(store_dir, exist_ok=True)
    policy_file = f"{workspace}/policy.json"
    data_file = f"{workspace}/data.json"
    with open(policy_file, "w") as f:
        json.dump({"rules": [{"path": "name", "action": "allow"},
                             {"path": "ssn", "action": "deny"}]}, f)
    with open(data_file, "w") as f:
        json.dump({"name": "A", "ssn": "1"}, f)
    _run(workspace, "policy", "publish", "--store-dir", store_dir,
         "--id", "p", "--file", policy_file)
    bundle_file = f"{workspace}/b.json"
    _run(workspace, "export", "--store-dir", store_dir,
         "--data", data_file, "--policy-id", "p", "--purpose", "analytics",
         "--out", bundle_file)
    with open(bundle_file) as f:
        b = json.load(f)
    b["output"]["name"] = "INTRUDER"
    with open(bundle_file, "w") as f:
        json.dump(b, f)
    proc = subprocess.run(
        [sys.executable, "-m", "mde.cli", "verify",
         "--store-dir", store_dir, "--bundle", bundle_file],
        cwd=workspace, capture_output=True, text=True, env=_ENV)
    assert proc.returncode == 3  # 核验失败退出码
    report = json.loads(proc.stdout)
    assert report["ok"] is False
