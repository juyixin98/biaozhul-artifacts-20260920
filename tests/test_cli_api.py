"""CLI and remaining HTTP endpoint coverage."""
from __future__ import annotations

import json
import os
import subprocess
import sys

from fastapi.testclient import TestClient

from adam_shards.adam import AdamConfig
from adam_shards.api import create_app
from adam_shards.config import AppConfig
from adam_shards.sharding import save_checkpoint


def _client(tmp_path):
    cfg = AppConfig(
        checkpoint_dir=str(tmp_path / "ck"), fixture_path="x", seed=1,
        layer_dims=(3, 2), batch_size=8, train_steps=2, adam=AdamConfig(),
        save_world_size=2, restore_world_size=3, tight_tol=1e-12,
        loose_tol=1e-8,
    )
    return TestClient(create_app(cfg))


def test_manifest_endpoint_serves_committed_checkpoint(small_state, tmp_path):
    params, moments = small_state
    ckpt_dir = tmp_path / "real_ckpt"
    commit = save_checkpoint(str(ckpt_dir), params, moments, world_size=2)
    client = _client(tmp_path)
    rid = "req-manifest"
    resp = client.post(
        "/checkpoints/manifest",
        headers={"X-Request-ID": rid},
        json={"ckpt_dir": str(ckpt_dir)},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["request_id"] == rid
    assert body["manifest"]["commit_id"] == commit
    assert body["manifest"]["world_size"] == 2


def test_manifest_endpoint_missing_dir_is_typed_error(tmp_path):
    client = _client(tmp_path)
    resp = client.post(
        "/checkpoints/manifest",
        headers={"X-Request-ID": "req-miss"},
        json={"ckpt_dir": str(tmp_path / "absent")},
    )
    assert resp.status_code == 422
    assert resp.json()["category"] == "missing_shard"


def test_cli_runs_pipeline_and_reports_pass():
    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    env = dict(os.environ, PYTHONPATH=os.path.join(repo_root, "src"))
    proc = subprocess.run(
        [sys.executable, "-m", "adam_shards",
         "--config", "configs/default.json",
         "--save-world", "2", "--restore-world", "3"],
        cwd=repo_root, env=env, capture_output=True, text=True, timeout=120,
    )
    assert proc.returncode == 0, proc.stderr
    # JSON object is the first document; SUMMARY follows.
    payload = json.loads(proc.stdout[:proc.stdout.index("\n}") + 2])
    assert payload["passed"] is True
    assert payload["save_world_size"] == 2
    assert payload["restore_world_size"] == 3
    assert "OVERALL PASSED      : True" in proc.stdout
