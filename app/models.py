"""快照输入模型（本地对象快照）。

快照模拟 Kubernetes 中 apiserver 可见的对象集合：Node / Pod / Deployment / PDB。
所有字段均为普通可序列化类型，便于做规范化哈希与签名。
"""
from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, Field, model_validator

_PERCENT_RE = re.compile(r"^(\d{1,3})%$")

PodPhase = Literal["Running", "Pending", "Succeeded", "Failed"]
OwnerKind = Literal["Deployment", "DaemonSet", "ReplicaSet", "Node"]


class PodSpec(BaseModel):
    name: str
    uid: str
    namespace: str = "default"
    node: str
    phase: PodPhase = "Running"
    ready: bool = False
    labels: dict[str, str] = Field(default_factory=dict)
    # owner_kind=None 表示裸 Pod（无控制器）；"Node" 表示 mirror pod
    owner_kind: OwnerKind | None = None
    owner_name: str | None = None
    controller: bool = True
    has_empty_dir: bool = False
    deletion_timestamp: str | None = None


class NodeSpec(BaseModel):
    name: str
    schedulable: bool = True
    # 模拟器调度槽位：用于构造“无处可调度”的故障场景
    capacity: int = 64
    pods: list[PodSpec] = Field(default_factory=list)


class DeploymentSpec(BaseModel):
    name: str
    namespace: str = "default"
    replicas: int = Field(ge=0)
    ready_replicas: int = Field(default=0, ge=0)
    selector: dict[str, str]
    # 新副本的标签，默认等于 selector
    template_labels: dict[str, str] | None = None
    # 模拟器：新建副本经过多少 tick 后才 Ready
    delay_ready_ticks: int = Field(default=1, ge=0)


class PdbSpec(BaseModel):
    name: str
    namespace: str = "default"
    selector: dict[str, str]
    # 二者必须且只能设置一个；整数或百分比字符串（如 "25%"）
    min_available: int | str | None = None
    max_unavailable: int | str | None = None

    @model_validator(mode="after")
    def _exactly_one_strategy(self) -> "PdbSpec":
        if (self.min_available is None) == (self.max_unavailable is None):
            raise ValueError(
                f"pdb {self.name}: min_available 与 max_unavailable 必须且只能设置一个"
            )
        for name, value in (
            ("min_available", self.min_available),
            ("max_unavailable", self.max_unavailable),
        ):
            if isinstance(value, str):
                m = _PERCENT_RE.match(value)
                if not m or not 0 <= int(m.group(1)) <= 100:
                    raise ValueError(f"pdb {self.name}: 非法百分比 {name}={value!r}")
            elif isinstance(value, int) and value < 0:
                raise ValueError(f"pdb {self.name}: {name} 不能为负数")
        return self


class SnapshotSpec(BaseModel):
    snapshot_id: str
    nodes: list[NodeSpec]
    deployments: list[DeploymentSpec] = Field(default_factory=list)
    pdbs: list[PdbSpec] = Field(default_factory=list)

    @model_validator(mode="after")
    def _check_pod_nodes(self) -> "SnapshotSpec":
        node_names = {n.name for n in self.nodes}
        for node in self.nodes:
            for pod in node.pods:
                if pod.node != node.name:
                    raise ValueError(
                        f"pod {pod.name} 声明 node={pod.node}，但位于 node {node.name} 下"
                    )
                if pod.namespace == "":
                    raise ValueError(f"pod {pod.name}: namespace 不能为空")
        dup = {n for n in node_names if list(x.name for x in self.nodes).count(n) > 1}
        if dup:
            raise ValueError(f"node 重名: {sorted(dup)}")
        return self
