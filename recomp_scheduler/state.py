"""训练状态：输入夹具、可训练参数、RNG 快照。

:class:`TrainState` 是一次训练步所需的全部"外部数据"：

- 输入节点的具体值（合成夹具，调用方提供）；
- 可训练参数（linear 层的 W/b），可用固定种子确定性初始化；
- 一个全局 :class:`numpy.random.Generator`，前向按拓扑序从中取随机数；
- 损失上游梯度（grad_outputs）。

引擎在每个检查点块边界调用 :meth:`TrainState.rng_snapshot` /
:meth:`TrainState.restore_rng`，从而保证重计算时 dropout 掩码逐位复现。
快照与恢复成对计数，:meth:`TrainState.snapshot_balance` 可在收尾时检查账目，
未恢复的快照属于状态冲突。
"""

from __future__ import annotations

import copy
import pickle
from dataclasses import dataclass, field
from typing import Any, Mapping

import numpy as np

from .errors import GraphValidationError, StateConflictError
from .graph import Graph

RNG_DTYPE = np.float64


@dataclass
class TrainState:
    graph: Graph
    inputs: dict[str, np.ndarray]
    params: dict[str, dict[str, np.ndarray]]
    grad_outputs: dict[str, np.ndarray]
    seed: int = 0
    rng: np.random.Generator = field(init=False)
    _snapshots_taken: int = field(default=0, init=False)
    _snapshots_restored: int = field(default=0, init=False)
    _restored_blocks: set[int] = field(default_factory=set, init=False)
    _advanced: bool = field(default=False, init=False)

    def __post_init__(self) -> None:
        self.rng = np.random.default_rng(self.seed)
        self.validate()

    # ------------------------------------------------------------------ #
    # 校验
    # ------------------------------------------------------------------ #
    def validate(self) -> None:
        """检查夹具与图完全匹配：形状、dtype、输入齐全、参数齐全。"""
        for inp_id in self.graph.inputs:
            if inp_id not in self.inputs:
                raise GraphValidationError("missing input feed", node=inp_id)
            data = self.inputs[inp_id]
            expected = self.graph.node(inp_id).shape
            if tuple(data.shape) != tuple(expected):
                raise GraphValidationError(
                    "input feed shape mismatch",
                    node=inp_id,
                    expected=tuple(expected),
                    actual=tuple(data.shape),
                )
            if data.dtype != RNG_DTYPE:
                raise GraphValidationError(
                    "input feed must be float64",
                    node=inp_id,
                    actual=str(data.dtype),
                )
            if not np.isfinite(data).all():
                raise GraphValidationError("input feed contains non-finite values", node=inp_id)

        for nid, node in self.graph.nodes.items():
            if not node.params:
                continue
            if nid not in self.params:
                raise GraphValidationError("missing params for node", node=nid)
            bucket = self.params[nid]
            names = [p.name for p in node.params]
            if set(bucket) != set(names):
                raise GraphValidationError(
                    "param names mismatch",
                    node=nid,
                    expected=names,
                    actual=sorted(bucket),
                )
            for spec in node.params:
                arr = bucket[spec.name]
                if tuple(arr.shape) != tuple(spec.shape):
                    raise GraphValidationError(
                        "param shape mismatch",
                        node=nid,
                        param=spec.name,
                        expected=tuple(spec.shape),
                        actual=tuple(arr.shape),
                    )

        for out_id in self.graph.outputs:
            if out_id not in self.grad_outputs:
                raise GraphValidationError("missing grad_output", node=out_id)
            g = self.grad_outputs[out_id]
            if tuple(g.shape) != tuple(self.graph.node(out_id).shape):
                raise GraphValidationError(
                    "grad_output shape mismatch",
                    node=out_id,
                    expected=tuple(self.graph.node(out_id).shape),
                    actual=tuple(g.shape),
                )

    # ------------------------------------------------------------------ #
    # RNG 快照/恢复（检查点重放的核心）
    # ------------------------------------------------------------------ #
    def rng_snapshot(self, block_id: int) -> bytes:
        """记录当前 RNG 状态为不可变 token（pickle bytes）。"""
        token = pickle.dumps(copy.deepcopy(self.rng.bit_generator.state))
        self._snapshots_taken += 1
        return token

    def restore_rng(self, token: bytes, block_id: int) -> None:
        """把 RNG 恢复到快照；同一块只允许恢复一次（重复恢复=状态冲突）。

        去重以**块号**为准：相邻检查点块之间若没有任何随机算子，
        两份快照字节可能完全相同，但它们仍是各自块独立重放所需的入口状态。
        """
        if block_id in self._restored_blocks:
            raise StateConflictError(
                "RNG snapshot restored twice for the same block",
                block=block_id,
            )
        self._restored_blocks.add(block_id)
        self.rng = np.random.default_rng()
        self.rng.bit_generator.state = pickle.loads(token)
        self._snapshots_restored += 1

    def snapshot_balance(self) -> dict[str, int]:
        return {
            "taken": self._snapshots_taken,
            "restored": self._snapshots_restored,
        }

    def mark_forward_done(self) -> None:
        if self._advanced:
            raise StateConflictError("forward pass already advanced this state")
        self._advanced = True

    # ------------------------------------------------------------------ #
    # 构造辅助
    # ------------------------------------------------------------------ #
    @classmethod
    def synthetic(
        cls,
        graph: Graph,
        *,
        seed: int = 1234,
        param_scale: float = 0.5,
        ones_grad: bool = True,
    ) -> "TrainState":
        """用固定种子生成全套合成夹具（输入/参数/上游梯度全 1 或确定性随机）。"""
        feed_rng = np.random.default_rng(seed)
        inputs: dict[str, np.ndarray] = {}
        for inp_id in graph.inputs:
            shape = graph.node(inp_id).shape
            inputs[inp_id] = feed_rng.standard_normal(shape).astype(RNG_DTYPE)

        params: dict[str, dict[str, np.ndarray]] = {}
        for nid, node in graph.nodes.items():
            if not node.params:
                continue
            bucket = {}
            for spec in node.params:
                bucket[spec.name] = (
                    feed_rng.standard_normal(spec.shape).astype(RNG_DTYPE) * param_scale
                )
            params[nid] = bucket

        grad_outputs = {
            oid: np.ones(graph.node(oid).shape, dtype=RNG_DTYPE)
            if ones_grad
            else feed_rng.standard_normal(graph.node(oid).shape).astype(RNG_DTYPE)
            for oid in graph.outputs
        }
        return cls(
            graph=graph,
            inputs=inputs,
            params=params,
            grad_outputs=grad_outputs,
            seed=seed,
        )

    def param_vector(self) -> np.ndarray:
        """把全部参数拼成一维向量（数值验证中有限差分会使用）。"""
        pieces = []
        for nid in self.graph.order:
            for spec in self.graph.node(nid).params:
                pieces.append(self.params[nid][spec.name].ravel())
        if not pieces:
            return np.zeros(0)
        return np.concatenate(pieces)

    def clone(self) -> "TrainState":
        """深拷贝（独立参考计算需要互不干扰的状态）。"""
        return TrainState(
            graph=self.graph,
            inputs={k: v.copy() for k, v in self.inputs.items()},
            params={
                nid: {p: a.copy() for p, a in bucket.items()}
                for nid, bucket in self.params.items()
            },
            grad_outputs={k: v.copy() for k, v in self.grad_outputs.items()},
            seed=self.seed,
        )


def assert_mapping(obj: Any, where: str) -> Mapping:
    if not isinstance(obj, Mapping):
        raise GraphValidationError(f"{where} must be an object")
    return obj
