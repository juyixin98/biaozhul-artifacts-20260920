"""数据模型定义（纯 Python，不依赖 rclpy，便于离线单测与示例输入）。"""

from __future__ import annotations

from enum import Enum
from typing import Literal, Optional

from pydantic import BaseModel, Field

# ---------------------------------------------------------------------------
# 枚举 / 字面量
# ---------------------------------------------------------------------------

ReliabilityKind = Literal["reliable", "best_effort", "system_default", "unknown", "best_available"]
DurabilityKind = Literal["volatile", "transient_local", "system_default", "unknown", "best_available"]
HistoryKind = Literal["keep_last", "keep_all", "system_default", "unknown"]
EndpointType = Literal["publisher", "subscription"]
EndpointState = Literal["discovered", "suspected_missing", "lost"]
Severity = Literal["incompatible", "risk", "ok"]
# 诊断结论：确定不兼容 / 仅性能风险 / 兼容 / 证据不足（端点未全部发现）
TopicVerdict = Literal["incompatible", "risk", "compatible", "insufficient_evidence"]


class QoSValues(BaseModel):
    """单个端点的 QoS 四元组（reliability/durability/history/depth）。"""

    reliability: ReliabilityKind = "unknown"
    durability: DurabilityKind = "unknown"
    history: HistoryKind = "unknown"
    depth: Optional[int] = None


class Endpoint(BaseModel):
    """一个发布/订阅端点（对应 DDS 里的一个 DataWriter/DataReader）。"""

    topic: str
    endpoint_type: EndpointType
    node_name: str
    node_namespace: str = "/"
    endpoint_gid: str
    topic_type: str = ""
    qos: QoSValues = Field(default_factory=QoSValues)

    first_seen: float
    last_seen: float
    state: EndpointState = "discovered"
    # state != discovered 时，记录第一次未被发现的时刻；用于“短暂未发现”判定
    missing_since: Optional[float] = None

    @property
    def key(self) -> str:
        return f"{self.endpoint_type}:{self.node_namespace}{self.node_name}:{self.endpoint_gid}:{self.topic}"


class EndpointEvent(BaseModel):
    """端点发现 / 消失事件（带时间戳，便于审计）。"""

    endpoint_key: str
    topic: str
    event: Literal["discovered", "rediscovered", "lost"]
    timestamp: float
    endpoint: Endpoint


# ---------------------------------------------------------------------------
# 诊断输出
# ---------------------------------------------------------------------------

class MatchChain(BaseModel):
    """可解释匹配链路：说明某条规则如何从两个端点的取值推出结论。"""

    rule_id: str
    title: str
    severity: Severity
    publisher_value: Optional[str] = None
    subscription_value: Optional[str] = None
    expected: Optional[str] = None
    detail: str


class Finding(BaseModel):
    rule_id: str
    severity: Severity
    title: str
    message: str
    chain: MatchChain


class PairDiagnosis(BaseModel):
    """一个 (publisher, subscription) 配对的诊断结果。"""

    publisher: Endpoint
    subscription: Endpoint
    verdict: Literal["incompatible", "risk", "compatible"]
    findings: list[Finding]


class TopicDiagnosis(BaseModel):
    """一个话题的聚合诊断。端点未凑齐时给 insufficient_evidence，绝不臆断为故障。"""

    topic: str
    topic_type: str
    publisher_count: int
    subscription_count: int
    verdict: TopicVerdict
    pairs: list[PairDiagnosis]
    # 无法参与配对分析的端点（例如只有发布方 / 端点处于疑似消失状态）
    unmatched_publishers: list[Endpoint] = []
    unmatched_subscriptions: list[Endpoint] = []


class Topology(BaseModel):
    """某一时刻采集到的全部端点。"""

    captured_at: float
    discovery_interval: float
    missing_grace_seconds: float
    endpoints: list[Endpoint] = Field(default_factory=list)
    events: list[EndpointEvent] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# 快照持久化
# ---------------------------------------------------------------------------

class Snapshot(BaseModel):
    """拓扑快照 + 当时的诊断结果 + 规则版本。"""

    schema_version: int = 1
    snapshot_id: str
    created_at: float
    created_iso: str
    rules_version: str
    topology: Topology
    diagnoses: list[TopicDiagnosis]


class SnapshotEnvelope(BaseModel):
    """落盘信封：payload 为快照的规范化 JSON 字节，附真实 SHA-256 与 HMAC-SHA256。"""

    snapshot_id: str
    sha256: str
    hmac_sha256: str
    hmac_key_id: str
    payload: str
