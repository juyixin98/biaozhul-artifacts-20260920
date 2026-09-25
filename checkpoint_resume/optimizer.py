"""带动量的 SGD 优化器。

动量缓冲（velocity）是优化器状态：只存权重而丢弃它，
恢复后的轨迹会立即偏离不中断训练 —— 测试中会专门验证这一点。
"""

from __future__ import annotations

import numpy as np

from .model import LinearModel


class MomentumSGD:
    def __init__(self, lr: float, momentum: float, weight_decay: float = 0.0):
        if lr <= 0:
            raise ValueError("lr 必须为正")
        if not 0.0 <= momentum < 1.0:
            raise ValueError("momentum 必须在 [0, 1) 内")
        self.lr = lr
        self.momentum = momentum
        self.weight_decay = weight_decay
        self.v_w: np.ndarray | None = None
        self.v_b: np.ndarray | None = None

    def step(self, model: LinearModel, grad_w: np.ndarray, grad_b: np.ndarray) -> None:
        if self.v_w is None:
            self.v_w = np.zeros_like(model.w)
            self.v_b = np.zeros_like(model.b)
        gw = grad_w + self.weight_decay * model.w
        gb = grad_b + self.weight_decay * model.b
        self.v_w = self.momentum * self.v_w - self.lr * gw
        self.v_b = self.momentum * self.v_b - self.lr * gb
        model.w = model.w + self.v_w
        model.b = model.b + self.v_b

    def state_dict(self) -> dict:
        if self.v_w is None:
            raise RuntimeError("优化器尚未执行任何 step，没有状态可保存")
        return {"opt_vw": self.v_w.copy(), "opt_vb": self.v_b.copy()}

    def load_state_dict(self, state: dict) -> None:
        self.v_w = np.asarray(state["opt_vw"], dtype=np.float64).copy()
        self.v_b = np.asarray(state["opt_vb"], dtype=np.float64).copy()
