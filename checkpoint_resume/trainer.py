"""训练循环：把模型、优化器、RNG、数据游标组装成可断点恢复的整体。"""

from __future__ import annotations

import hashlib
import time
from pathlib import Path

import numpy as np

from . import checkpoint as ckpt
from .config import TrainConfig
from .data import BatchLoader, make_synthetic_regression
from .model import LinearModel, mse_loss_and_grads
from .optimizer import MomentumSGD

# 恢复时必须与检查点一致的配置字段，否则说明在恢复「另一个运行」
_MUST_MATCH = ("n_samples", "n_features", "batch_size", "seed")


class Trainer:
    """确定性训练器：同一 config 下，中断恢复与不中断轨迹逐位一致。

    resume=True 时自动从 checkpoint_dir 中「最新的有效检查点」恢复；
    最新检查点损坏时回退到次新的有效检查点；全部损坏则从头训练。
    """

    def __init__(self, config: TrainConfig, resume: bool = True):
        self.config = config
        self.rng = np.random.RandomState(config.seed)
        X, y = make_synthetic_regression(config.n_samples, config.n_features, config.seed)
        self.X, self.y = X, y
        self.model = LinearModel(config.n_features, self.rng)
        self.optimizer = MomentumSGD(config.lr, config.momentum, config.weight_decay)
        self.loader = BatchLoader(X, y, config.batch_size, self.rng)
        self.global_step = 0
        self.history: list[dict] = []
        self.resumed_from: str | None = None

        if resume:
            latest = ckpt.find_latest_valid(config.checkpoint_dir)
            if latest is not None:
                self._restore(latest)

    # ---- 状态快照 ----

    def _rng_state_arrays(self) -> dict:
        name, keys, pos, has_gauss, cached = self.rng.get_state()
        if name != "MT19937":
            raise RuntimeError(f"不支持的 RNG 类型: {name}")
        return {
            "rng_keys": keys,
            "rng_pos": np.int64(pos),
            "rng_has_gauss": np.int64(has_gauss),
            "rng_cached_gauss": np.float64(cached),
        }

    def _restore_rng(self, state: dict) -> None:
        self.rng.set_state(
            (
                "MT19937",
                np.asarray(state["rng_keys"], dtype=np.uint32),
                int(state["rng_pos"]),
                int(state["rng_has_gauss"]),
                float(state["rng_cached_gauss"]),
            )
        )

    def state_dict(self) -> dict:
        state = {}
        state.update(self.model.state_dict())
        state.update(self.optimizer.state_dict())
        state.update(self._rng_state_arrays())
        loader_state = self.loader.state_dict()
        state["perm"] = loader_state["permutation"]
        state["epoch"] = np.int64(loader_state["epoch"])
        state["next_batch"] = np.int64(loader_state["next_batch"])
        state["global_step"] = np.int64(self.global_step)
        return state

    def save(self) -> Path:
        if self.global_step == 0:
            raise RuntimeError("尚未训练任何 step，拒绝保存空检查点")
        meta = {
            "config": self.config.to_dict(),
            "saved_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        }
        path = ckpt.save_checkpoint(
            self.state_dict(), meta, self.config.checkpoint_dir, self.global_step
        )
        ckpt.prune_checkpoints(self.config.checkpoint_dir, self.config.keep_last)
        return path

    def _restore(self, path: Path) -> None:
        state, meta = ckpt.load_checkpoint(path)
        saved_cfg = meta.get("config", {})
        for key in _MUST_MATCH:
            if saved_cfg.get(key) != getattr(self.config, key):
                raise ValueError(
                    f"检查点与当前配置不匹配: {key} "
                    f"(检查点={saved_cfg.get(key)!r}, 当前={getattr(self.config, key)!r})"
                )
        self.model.load_state_dict(state)
        self.optimizer.load_state_dict(state)
        self._restore_rng(state)
        self.loader.load_state_dict(
            {
                "epoch": state["epoch"],
                "next_batch": state["next_batch"],
                "permutation": state["perm"],
            }
        )
        self.global_step = int(state["global_step"])
        self.resumed_from = str(path)

    # ---- 训练 ----

    def step(self) -> dict:
        """执行一个训练 step，返回该步的记录。"""
        Xb, yb, idx = self.loader.next()
        loss, grad_w, grad_b = mse_loss_and_grads(self.model, Xb, yb)
        self.optimizer.step(self.model, grad_w, grad_b)
        self.global_step += 1
        record = {
            "step": self.global_step,
            "epoch": self.loader.epoch,
            "batch_idx": self.loader.next_batch - 1,
            "loss": loss,
            "batch_hash": hashlib.sha1(idx.tobytes()).hexdigest()[:12],
        }
        self.history.append(record)
        return record

    def train(self, max_steps: int) -> list[dict]:
        """训练 max_steps 个 step（模拟被「中断」就是 max_steps 用完后进程退出）。"""
        if max_steps <= 0:
            raise ValueError("max_steps 必须为正")
        records = []
        for _ in range(max_steps):
            records.append(self.step())
            if self.global_step % self.config.checkpoint_every == 0:
                self.save()
        return records

    def full_loss(self) -> float:
        """全数据集上的 MSE，用于最终对比（不消耗 RNG，不影响轨迹）。"""
        err = self.model.predict(self.X) - self.y
        return float(np.mean(err * err))

    def status(self) -> dict:
        return {
            "global_step": self.global_step,
            "epoch": self.loader.epoch,
            "next_batch": self.loader.next_batch,
            "steps_per_epoch": self.loader.n_batches,
            "resumed_from": self.resumed_from,
            "full_loss": self.full_loss(),
        }
