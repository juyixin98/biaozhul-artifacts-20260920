"""配置加载测试。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from gradbucket.config import RunConfig, load_config


CONFIG_DIR = Path(__file__).resolve().parent.parent / "configs"


def test_load_shipped_unequal_shards_config():
    cfg = load_config(CONFIG_DIR / "unequal_shards.json")
    assert cfg.n_workers == 3
    assert cfg.shard_sizes == [2, 3, 7]
    assert sum(cfg.shard_sizes) == cfg.n_samples
    assert cfg.bucket_capacity == 2


def test_unknown_field_rejected(tmp_path):
    p = tmp_path / "bad.json"
    p.write_text(json.dumps({"n_workers": 2, "bogus": 1}), encoding="utf-8")
    with pytest.raises(ValueError, match="未知字段"):
        load_config(p)


def test_inconsistent_shard_sizes_rejected():
    with pytest.raises(ValueError, match="shard_sizes"):
        RunConfig(n_workers=3, shard_sizes=[1, 1, 9])  # 和 != 12


def test_invalid_capacity_and_timeout_rejected():
    with pytest.raises(ValueError, match="bucket_capacity"):
        RunConfig(bucket_capacity=0)
    with pytest.raises(ValueError, match="liveness_timeout"):
        RunConfig(liveness_timeout=-1.0)


def test_cluster_kwargs_maps_worker_overrides():
    from gradbucket.config import cluster_kwargs

    cfg = load_config(CONFIG_DIR / "worker_lost.json")
    kwargs = cluster_kwargs(cfg)
    assert kwargs["shard_sizes"] == [2, 3, 7]
    assert kwargs["liveness_timeout"] == 1.5
    specs = {s.worker_id: s for s in kwargs["workers"]}
    assert set(specs) == {"w0", "w1", "w2"}
    assert specs["w2"].fail_after_bucket == 0
    assert specs["w0"].fail_after_bucket == -1
