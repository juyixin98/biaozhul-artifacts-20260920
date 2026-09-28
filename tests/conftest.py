"""共享测试夹具与独立小预言机。

注意：这里的 ``explicit_weighted_average`` 与 ``finite_difference_grad``
是**测试自带**的朴素实现，刻意与生产代码
（``reducer`` / ``graph.LinearModel``）独立，避免"答案由被测核心自己生成"。
"""

from __future__ import annotations

import numpy as np
import pytest

from gradbucket.graph import LinearModel, ModelConfig, make_dataset, shard_indices
from gradbucket.tensors import build_layout
from gradbucket.runtime import FakeClock
from gradbucket.training import RoundCoordinator
from gradbucket.diagnostics import DiagnosticLog


@pytest.fixture
def cfg():
    return ModelConfig(n_in=3, n_out=2, include_spare=True)


@pytest.fixture
def model(cfg):
    return LinearModel(cfg)


@pytest.fixture
def params(model):
    return model.param_specs()


@pytest.fixture
def layout(params):
    return build_layout(0, params, bucket_capacity=2)


@pytest.fixture
def data():
    x, y = make_dataset(12, seed=445, n_in=3, n_out=2)
    return x, y


@pytest.fixture
def three_shards(data):
    x, y = data
    return [
        (x[shard_indices(12, r, 3, seed=445)],
         y[shard_indices(12, r, 3, seed=445)])
        for r in range(3)
    ]


@pytest.fixture
def coordinator():
    return RoundCoordinator(liveness_timeout=5.0, clock=FakeClock(),
                            diaglog=DiagnosticLog())


def explicit_weighted_average(pairs):
    """测试自带预言机：pairs=[(n, vec), ...] -> 按真实 n 的逐元素加权平均。

    纯 Python 循环 + float 求和，与 numpy/reducer 的实现路径完全不同。
    """
    if not pairs:
        raise ValueError("无贡献")
    length = len(pairs[0][1])
    total_n = sum(n for n, _ in pairs)
    out = [0.0] * length
    for n, vec in pairs:
        for i, v in enumerate(vec):
            out[i] += float(n) * float(v)
    return np.array([v / total_n for v in out], dtype=np.float64)


def finite_difference_grad(model, x, y, param_name, eps=1e-7):
    """测试自带预言机：中心差分数值梯度，独立于解析式推导。"""
    def loss_with(tensor):
        old = getattr(model, param_name).copy()
        setattr(model, param_name, tensor)
        val = model.loss(x, y)
        setattr(model, param_name, old)
        return val

    tensor = getattr(model, param_name).copy()
    grad = np.zeros_like(tensor)
    it = np.ndindex(tensor.shape)
    for idx in it:
        perturbed = tensor.copy()
        perturbed[idx] += eps
        plus = loss_with(perturbed)
        perturbed = tensor.copy()
        perturbed[idx] -= eps
        minus = loss_with(perturbed)
        grad[idx] = (plus - minus) / (2 * eps)
    return grad
