"""计算图模块。

职责：
- 一个最小但真实的计算图：多输出线性模型 ``f(x) = W x + b``，MSE 损失。
- 可复现的**本地合成数据**（由种子生成，无外部数据、无账号）。
- 基于**原始样本**计算该工作者分片上的平均损失梯度，并返回真实样本数。
- 声明一个不进入损失的 ``spare`` 参数：它的真实梯度恒为零，工作者可能
  "没算它"——用于驱动显式占位与缺遮罩路径。

本模块不知道分桶、归约、网络或轮次的存在；它只产出
``(梯度 dict, 样本数)``。
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from .tensors import ParamSpec


@dataclass(frozen=True)
class ModelConfig:
    n_in: int = 3
    n_out: int = 2
    # 不参与损失的参数；真实梯度为零，用于占位/缺梯度路径。
    include_spare: bool = True


class LinearModel:
    """f(x) = W x + b，逐样本 0.5 * ||f - y||^2 的均值损失。

    梯度定义（N = 该分片样本数）::

        d_i = (W x_i + b - y_i) / N
        gW = sum_i d_i x_i^T
        gb = sum_i d_i
        g_spare = 0
    """

    def __init__(self, config: ModelConfig | None = None) -> None:
        self.config = config or ModelConfig()
        self.w = np.zeros((self.config.n_out, self.config.n_in), dtype=np.float64)
        self.b = np.zeros(self.config.n_out, dtype=np.float64)
        self.spare = np.zeros(self.config.n_out, dtype=np.float64)

    # -- 参数声明：轮内参数顺序的唯一来源 -------------------------------

    def param_specs(self) -> list[ParamSpec]:
        specs = [
            ParamSpec("w", (self.config.n_out, self.config.n_in)),
            ParamSpec("b", (self.config.n_out,)),
        ]
        if self.config.include_spare:
            specs.append(ParamSpec("spare", (self.config.n_out,)))
        return specs

    def set_weights(self, w: np.ndarray, b: np.ndarray) -> None:
        self.w = np.array(w, dtype=np.float64, copy=True).reshape(self.w.shape)
        self.b = np.array(b, dtype=np.float64, copy=True).reshape(self.b.shape)

    def forward(self, x: np.ndarray) -> np.ndarray:
        x = np.asarray(x, dtype=np.float64)
        return x @ self.w.T + self.b

    def loss(self, x: np.ndarray, y: np.ndarray) -> float:
        pred = self.forward(x)
        return float(0.5 * np.mean(np.sum((pred - y) ** 2, axis=1)))

    def gradients(
        self, x: np.ndarray, y: np.ndarray
    ) -> tuple[dict[str, np.ndarray], int]:
        """返回 (平均损失梯度 dict, 真实样本数)。

        空分片是调用错误：没有样本就没有"该分片的平均梯度"，必须在边界拒绝，
        而不是悄悄产出零梯度污染加权平均。
        """
        x = np.asarray(x, dtype=np.float64)
        y = np.asarray(y, dtype=np.float64)
        if x.ndim != 2 or x.shape[1] != self.config.n_in:
            raise ValueError(f"x 形状应为 (N, {self.config.n_in})，收到 {x.shape}")
        n = x.shape[0]
        if n == 0:
            raise ValueError("空分片无法定义平均梯度")
        if y.shape != (n, self.config.n_out):
            raise ValueError(f"y 形状应为 {(n, self.config.n_out)}，收到 {y.shape}")
        delta = (self.forward(x) - y) / n  # (N, n_out)
        gw = delta.T @ x
        gb = delta.sum(axis=0)
        grads = {"w": gw, "b": gb}
        if self.config.include_spare:
            grads["spare"] = np.zeros_like(self.spare)
        return grads, n


def make_dataset(
    n_samples: int,
    *,
    seed: int = 445,
    n_in: int = 3,
    n_out: int = 2,
) -> tuple[np.ndarray, np.ndarray]:
    """生成本地合成回归数据 (X, Y)。

    Y 由一个隐藏线性映射 + 小噪声产生，全程确定性可复现。
    """
    rng = np.random.default_rng(seed)
    x = rng.normal(size=(n_samples, n_in))
    w_true = rng.normal(size=(n_out, n_in))
    noise = 0.05 * rng.normal(size=(n_samples, n_out))
    y = x @ w_true.T + noise
    return x, y


def shard_indices(
    n_samples: int,
    worker_rank: int,
    n_workers: int,
    *,
    shard_sizes: list[int] | None = None,
    seed: int = 445,
) -> np.ndarray:
    """把样本确定性地分给工作者。

    - 默认均匀连续分片（按打乱后的索引，避免数据顺序相关）。
    - ``shard_sizes`` 给出每个工作者的**真实样本数**用于不等批量测试，
      其和必须等于 ``n_samples``。
    """
    if not 0 <= worker_rank < n_workers:
        raise ValueError(f"worker_rank {worker_rank} 越界（共 {n_workers} 个工作者）")
    rng = np.random.default_rng(seed ^ 0x9E3779B9)
    perm = rng.permutation(n_samples)
    if shard_sizes is None:
        bounds = np.linspace(0, n_samples, n_workers + 1, dtype=int)
        return perm[bounds[worker_rank] : bounds[worker_rank + 1]]
    if len(shard_sizes) != n_workers or sum(shard_sizes) != n_samples:
        raise ValueError("shard_sizes 长度必须等于工作者数且总和等于样本总数")
    start = sum(shard_sizes[:worker_rank])
    return perm[start : start + shard_sizes[worker_rank]]
