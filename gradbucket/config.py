"""运行配置加载（JSON，无第三方依赖）。

配置文件位于仓库 ``configs/`` 目录；测试不依赖配置文件（夹具自带配置），
二者独立组织。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class RunConfig:
    n_workers: int = 3
    n_samples: int = 12
    shard_sizes: list[int] | None = None
    bucket_capacity: int = 2
    learning_rate: float = 0.1
    liveness_timeout: float = 3.0
    port: int = 8765
    seed: int = 445
    # 每工作者覆写：omit 参数 / 中断桶 / 压下桶
    workers: dict[str, dict] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.bucket_capacity <= 0:
            raise ValueError("bucket_capacity 必须为正")
        if self.liveness_timeout <= 0:
            raise ValueError("liveness_timeout 必须为正")
        if self.shard_sizes is not None:
            if len(self.shard_sizes) != self.n_workers:
                raise ValueError("shard_sizes 长度必须等于 n_workers")
            if sum(self.shard_sizes) != self.n_samples:
                raise ValueError("shard_sizes 之和必须等于 n_samples")


def load_config(path: str | Path) -> RunConfig:
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    known = {f for f in RunConfig.__dataclass_fields__}  # type: ignore[attr-defined]
    unknown = set(raw) - known
    if unknown:
        raise ValueError(f"配置含未知字段: {sorted(unknown)}")
    return RunConfig(**raw)


def cluster_kwargs(cfg: RunConfig) -> dict:
    """把配置映射为 :func:`gradbucket.cluster.run_cluster` 的关键字参数。

    ``workers`` 覆写形如::

        {"w2": {"fail_after_bucket": 0, "omit": "spare"}}
    """
    from .cluster import WorkerSpec

    specs = []
    for rank in range(cfg.n_workers):
        overrides = cfg.workers.get(f"w{rank}", {})
        specs.append(
            WorkerSpec(
                worker_id=f"w{rank}",
                rank=rank,
                omit=str(overrides.get("omit", "")),
                fail_after_bucket=int(overrides.get("fail_after_bucket", -1)),
                withhold_bucket=int(overrides.get("withhold_bucket", -1)),
                linger_seconds=float(overrides.get("linger_seconds", 1.5)),
            )
        )
    return {
        "n_workers": cfg.n_workers,
        "n_samples": cfg.n_samples,
        "shard_sizes": cfg.shard_sizes,
        "bucket_capacity": cfg.bucket_capacity,
        "liveness_timeout": cfg.liveness_timeout,
        "port": cfg.port,
        "workers": specs,
    }
