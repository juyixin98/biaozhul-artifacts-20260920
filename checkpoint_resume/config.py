"""训练配置。"""

from __future__ import annotations

from dataclasses import dataclass, asdict, field, fields


@dataclass
class TrainConfig:
    """一次训练运行的全部超参数。

    同一 config（含 seed）必须能复现同一条训练轨迹，
    这是断点恢复一致性的前提。
    """

    n_samples: int = 512          # 合成数据集样本数
    n_features: int = 8           # 特征维度
    batch_size: int = 32          # 批大小（不能整除时丢弃最后一个不满的批）
    lr: float = 0.05              # 学习率
    momentum: float = 0.9         # SGD 动量（>0 使优化器状态不可省略）
    weight_decay: float = 1e-4    # 权重衰减
    seed: int = 1234              # 全局随机种子
    checkpoint_every: int = 10    # 每隔多少个 step 自动保存检查点
    checkpoint_dir: str = "checkpoints"
    keep_last: int = 3            # 最多保留的最近检查点数

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "TrainConfig":
        known = {f.name for f in fields(cls)}
        unknown = set(data) - known
        if unknown:
            raise ValueError(f"未知配置字段: {sorted(unknown)}")
        return cls(**data)

    @property
    def steps_per_epoch(self) -> int:
        return self.n_samples // self.batch_size
