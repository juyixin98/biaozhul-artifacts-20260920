"""单进程联合批参照（独立答案来源）。

**这个模块是测试的"标准答案"，刻意不调用** :mod:`gradbucket.reducer`，
也不经过分桶/打包/网络。它直接把所有工作者的*原始样本*拼成一个联合批，
用 :class:`~gradbucket.graph.LinearModel` 在联合批上求一次平均梯度。

因此若 reducer 与本参照一致，就同时验证了打包/解包、分槽、加权、遮罩四条
路径；若两者共享实现，对照将毫无意义——所以这里保持最朴素的拼接求和，
便于人工核对。

另提供 :func:`hand_computed_constants`：一组小规模**手算**常数，测试直接
断言字面量，连参照自身都不能"自证"。
"""

from __future__ import annotations

import numpy as np

from .graph import LinearModel


def union_batch_gradient(
    model: LinearModel,
    shards: list[tuple[np.ndarray, np.ndarray]],
) -> tuple[dict[str, np.ndarray], int]:
    """把各分片原始样本纵向拼接，求联合批平均梯度与总样本数。

    定义上，g = (1/N) Σ_{所有样本} ...，N = Σ N_w。
    空分片列表或空分片直接报错——参照侧同样不容忍"无证据平均"。
    """
    if not shards:
        raise ValueError("没有任何分片，无法构成联合批")
    xs = [np.asarray(x, dtype=np.float64) for x, _ in shards]
    ys = [np.asarray(y, dtype=np.float64) for _, y in shards]
    if any(x.shape[0] == 0 for x in xs):
        raise ValueError("参照联合批不接受空分片")
    x_all = np.concatenate(xs, axis=0)
    y_all = np.concatenate(ys, axis=0)
    grads, n_total = model.gradients(x_all, y_all)
    return grads, n_total


def hand_computed_constants() -> dict:
    """可手算的常数（n_in=n_out=1，W=0，b=0，故 d_i = -y_i/N，x_i=1）。

    四个样本，分片大小 1/2/1（不等批量）::

        判别用 y = [10, 0, 0, 0]
          联合批:      gW = gb = -10/4        = -2.5
          w0(n=1):              -10/1         = -10
          w1(n=2):              -(0+0)/2      = 0
          w2(n=1):              0
          正确(按样本数加权):   (1*-10)/4     = -2.5
          错误(按工作者平均):   -10/3         ≈ -3.3333

        通用 y = [1, 2, 3, 4]
          联合批:      gW = gb = -(1+2+3+4)/4 = -2.5
          w0: -1 ; w1: -2.5 ; w2: -4
          加权: -10/4 = -2.5（此例按人头平均恰好相同，故仅作补充）
    """
    x = np.ones((4, 1), dtype=np.float64)
    return {
        "n_in": 1,
        "n_out": 1,
        "x": x,
        "shard_sizes": [1, 2, 1],
        "discriminator": {
            "y": np.array([[10.0], [0.0], [0.0], [0.0]]),
            "union_gw": -2.5,
            "union_gb": -2.5,
            "worker_gw": [-10.0, 0.0, 0.0],
            "weighted_gw": -2.5,
            "naive_worker_average_gw": -10.0 / 3.0,
        },
        "general": {
            "y": np.array([[1.0], [2.0], [3.0], [4.0]]),
            "union_gw": -2.5,
            "union_gb": -2.5,
            "worker_gw": [-1.0, -2.5, -4.0],
            "weighted_gw": -2.5,
        },
    }
