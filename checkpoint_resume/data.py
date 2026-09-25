"""可复现的合成回归数据与带游标的批次读取器。

不下载任何外部数据：y = X @ w_true + 噪声，全部由固定种子生成。
"""

from __future__ import annotations

import numpy as np


def make_synthetic_regression(
    n_samples: int, n_features: int, seed: int, noise_std: float = 0.1
) -> tuple[np.ndarray, np.ndarray]:
    """生成确定性的合成线性回归数据集。"""
    rng = np.random.RandomState(seed)
    true_w = rng.randn(n_features, 1)
    X = rng.randn(n_samples, n_features)
    y = X @ true_w + noise_std * rng.randn(n_samples, 1)
    return X, y


class BatchLoader:
    """按 (epoch, next_batch) 游标顺序产出批次。

    每个 epoch 开始时从共享 RNG 抽取一次排列（shuffle）。
    游标 + 当前排列 + RNG 状态共同构成可恢复的数据状态，
    三者都必须进入检查点，否则恢复后数据顺序会改变。
    """

    def __init__(self, X: np.ndarray, y: np.ndarray, batch_size: int, rng: np.random.RandomState):
        if batch_size <= 0:
            raise ValueError("batch_size 必须为正")
        self.X = X
        self.y = y
        self.batch_size = batch_size
        self.rng = rng
        self.n_batches = len(X) // batch_size  # 丢弃末尾不满一批的样本
        if self.n_batches == 0:
            raise ValueError("batch_size 大于样本数，没有任何完整批次")
        self.epoch = 0
        self.next_batch = 0
        self.permutation: np.ndarray | None = None

    def cursor(self) -> dict:
        return {"epoch": self.epoch, "next_batch": self.next_batch}

    def _start_new_epoch(self) -> None:
        self.permutation = self.rng.permutation(len(self.X))
        self.epoch += 1
        self.next_batch = 0

    def next(self) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        """返回 (X_batch, y_batch, indices)。游标推进一个批次。"""
        if self.permutation is None or self.next_batch >= self.n_batches:
            self._start_new_epoch()
        start = self.next_batch * self.batch_size
        idx = self.permutation[start : start + self.batch_size]
        self.next_batch += 1
        return self.X[idx], self.y[idx], idx

    def state_dict(self) -> dict:
        if self.permutation is None:
            raise RuntimeError("尚未产生任何批次，没有可保存的排列")
        return {
            "epoch": self.epoch,
            "next_batch": self.next_batch,
            "permutation": self.permutation,
        }

    def load_state_dict(self, state: dict) -> None:
        self.epoch = int(state["epoch"])
        self.next_batch = int(state["next_batch"])
        self.permutation = np.asarray(state["permutation"], dtype=np.int64)
        if not 0 <= self.next_batch <= self.n_batches:
            raise ValueError(
                f"游标越界: next_batch={self.next_batch}, n_batches={self.n_batches}"
            )
