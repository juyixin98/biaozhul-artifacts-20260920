"""QoS 兼容规则引擎。

判定依据（ROS 2 / DDS 规范，rmw QoS 兼容矩阵）：

* reliability（可靠性）：订阅方“请求”、发布方“提供”。
  订阅方请求 RELIABLE 时，发布方必须提供 RELIABLE，否则 **确定不兼容**（R-REL-001）；
  BEST_EFFORT 订阅方可以和任意发布方建立连接。
* durability（持久性）：订阅方请求 TRANSIENT_LOCAL 时，发布方必须提供 TRANSIENT_LOCAL，
  否则 **确定不兼容**（R-DUR-001）；VOLATILE 订阅方可与任意发布方连接。
* history / depth（历史策略与队列深度）不属于 DDS 线上不兼容维度，只会造成丢消息、
  内存增长等 **性能风险**（R-HIST-001 / R-DEPTH-001），绝不判为不兼容。
* 话题类型名不一致同样无法建立数据通路（R-TYPE-001，确定不兼容）。

每个判定都输出 MatchChain（取值、期望值、依据），形成可解释匹配链路；
通过项也输出 severity=ok 的记录，使检查过程完整可见。
"""

from __future__ import annotations

from .models import (
    Endpoint,
    Finding,
    MatchChain,
    PairDiagnosis,
    QoSValues,
    Topology,
    TopicDiagnosis,
)

RULES_VERSION = "qos-rules-1.0.0"

# 规则元数据：rule_id -> (标题, 默认严重级别, 依据说明)
RULE_CATALOG: dict[str, tuple[str, str, str]] = {
    "R-REL-001": (
        "可靠性兼容（reliability）",
        "incompatible",
        "订阅方请求 RELIABLE 时发布方必须提供 RELIABLE；BEST_EFFORT 发布方无法满足。",
    ),
    "R-DUR-001": (
        "持久性兼容（durability）",
        "incompatible",
        "订阅方请求 TRANSIENT_LOCAL（latched）时发布方必须提供 TRANSIENT_LOCAL。",
    ),
    "R-TYPE-001": (
        "话题类型一致（topic type）",
        "incompatible",
        "同一话题上发布方与订阅方的消息类型名必须一致，否则 DDS 不会建立数据通路。",
    ),
    "R-HIST-001": (
        "历史策略差异（history）",
        "risk",
        "KEEP_LAST / KEEP_ALL 差异不是线上不兼容项，但会改变缓存与丢弃行为，仅为性能风险。",
    ),
    "R-DEPTH-001": (
        "队列深度差异（depth）",
        "risk",
        "depth 只影响本端缓存长度，不影响建链；差异可能导致丢消息或多余内存占用，仅为性能风险。",
    ),
    "R-DISC-001": (
        "端点当前暂未发现（发现宽限期内）",
        "risk",
        "端点在发现宽限期内未被观察到，诊断基于最后已知 QoS；短暂未发现不等于永久故障。",
    ),
    "R-UNKNOWN-001": (
        "QoS 取值未知，无法完整确认",
        "risk",
        "端点回报 UNKNOWN/SYSTEM_DEFAULT，无法静态确认该维度，建议显式配置 QoS。",
    ),
}


def list_rules() -> list[dict[str, str]]:
    return [
        {"rule_id": rid, "title": t, "severity": s, "rationale": d}
        for rid, (t, s, d) in RULE_CATALOG.items()
    ]


def _finding(
    rule_id: str,
    severity: str,
    pub_value: str | None,
    sub_value: str | None,
    expected: str | None,
    detail: str,
) -> Finding:
    title, _, _ = RULE_CATALOG[rule_id]
    return Finding(
        rule_id=rule_id,
        severity=severity,  # type: ignore[arg-type]
        title=title,
        message=detail,
        chain=MatchChain(
            rule_id=rule_id,
            title=title,
            severity=severity,  # type: ignore[arg-type]
            publisher_value=pub_value,
            subscription_value=sub_value,
            expected=expected,
            detail=detail,
        ),
    )


def _active(endpoint: Endpoint) -> bool:
    """lost（超过宽限期确认消失）的端点不再参与配对。"""
    return endpoint.state in ("discovered", "suspected_missing")


def evaluate_pair(pub: Endpoint, sub: Endpoint) -> PairDiagnosis:
    """评估单个 (publisher, subscription) 配对。"""
    findings: list[Finding] = []
    pq: QoSValues = pub.qos
    sq: QoSValues = sub.qos

    # --- R-REL-001 可靠性 ---
    if "reliable" in (pq.reliability, sq.reliability) or "best_effort" in (
        pq.reliability,
        sq.reliability,
    ):
        if sq.reliability == "reliable" and pq.reliability == "best_effort":
            findings.append(
                _finding(
                    "R-REL-001",
                    "incompatible",
                    pq.reliability,
                    sq.reliability,
                    "publisher >= reliable（订阅方请求 reliable 时）",
                    "订阅方请求 RELIABLE，而发布方仅提供 BEST_EFFORT：DDS 拒绝建立该连接，"
                    "订阅方将收不到任何消息（确定不兼容）。",
                )
            )
        else:
            findings.append(
                _finding(
                    "R-REL-001",
                    "ok",
                    pq.reliability,
                    sq.reliability,
                    "publisher 可靠性不低于 subscription 请求",
                    f"发布方={pq.reliability} 满足订阅方={sq.reliability}，可靠性维度兼容。",
                )
            )

    # --- R-DUR-001 持久性 ---
    if "transient_local" in (pq.durability, sq.durability) or "volatile" in (
        pq.durability,
        sq.durability,
    ):
        if sq.durability == "transient_local" and pq.durability == "volatile":
            findings.append(
                _finding(
                    "R-DUR-001",
                    "incompatible",
                    pq.durability,
                    sq.durability,
                    "publisher 提供 transient_local（订阅方请求 latched 时）",
                    "订阅方请求 TRANSIENT_LOCAL（期望 latched 历史消息），发布方仅为 VOLATILE："
                    "DDS 拒绝建链（确定不兼容）。",
                )
            )
        else:
            findings.append(
                _finding(
                    "R-DUR-001",
                    "ok",
                    pq.durability,
                    sq.durability,
                    "publisher 持久性不低于 subscription 请求",
                    f"发布方={pq.durability} 满足订阅方={sq.durability}，持久性维度兼容。",
                )
            )

    # --- R-TYPE-001 类型一致 ---
    if pub.topic_type and sub.topic_type:
        if pub.topic_type != sub.topic_type:
            findings.append(
                _finding(
                    "R-TYPE-001",
                    "incompatible",
                    pub.topic_type,
                    sub.topic_type,
                    "publisher.topic_type == subscription.topic_type",
                    f"话题类型不一致：{pub.topic_type} vs {sub.topic_type}，DDS 不会建立数据通路。",
                )
            )
        else:
            findings.append(
                _finding(
                    "R-TYPE-001",
                    "ok",
                    pub.topic_type,
                    sub.topic_type,
                    "类型名一致",
                    f"双方类型均为 {pub.topic_type}。",
                )
            )

    # --- R-HIST-001 历史策略（仅风险） ---
    known_history = {pq.history, sq.history}
    if not known_history <= {"unknown", "system_default"}:
        if pq.history != sq.history and not (
            "unknown" in known_history or "system_default" in known_history
        ):
            findings.append(
                _finding(
                    "R-HIST-001",
                    "risk",
                    pq.history,
                    sq.history,
                    "非强制：差异不影响建链",
                    f"history 不同（发布方={pq.history}，订阅方={sq.history}），"
                    "不会导致不兼容，但缓存/丢弃语义不一致，存在性能风险。",
                )
            )
        else:
            findings.append(
                _finding(
                    "R-HIST-001",
                    "ok",
                    pq.history,
                    sq.history,
                    "非强制维度",
                    f"history 均为 {pq.history}，无历史策略差异风险。",
                )
            )

    # --- R-DEPTH-001 队列深度（仅 KEEP_LAST 时，仅风险） ---
    if (
        pq.history == "keep_last"
        and sq.history == "keep_last"
        and pq.depth is not None
        and sq.depth is not None
        and pq.depth != sq.depth
    ):
        if pq.depth < sq.depth:
            detail = (
                f"发布方 depth={pq.depth} 小于订阅方 depth={sq.depth}：发布队列更浅，"
                "慢订阅方场景下可能在发布侧被丢弃（仅性能风险，不影响建链）。"
            )
        else:
            detail = (
                f"发布方 depth={pq.depth} 大于订阅方 depth={sq.depth}："
                "订阅侧缓存较浅，突发时可能在订阅侧丢弃旧样本，并造成不必要的发布侧内存占用（仅性能风险）。"
            )
        findings.append(
            _finding(
                "R-DEPTH-001",
                "risk",
                str(pq.depth),
                str(sq.depth),
                "非强制：depth 差异不影响建链",
                detail,
            )
        )

    # --- R-UNKNOWN-001 未知 QoS ---
    unknown_dims: list[str] = []
    for dim, pv, sv in (
        ("reliability", pq.reliability, sq.reliability),
        ("durability", pq.durability, sq.durability),
        ("history", pq.history, sq.history),
    ):
        if pv in ("unknown", "system_default") or sv in ("unknown", "system_default"):
            unknown_dims.append(f"{dim}(pub={pv},sub={sv})")
    if unknown_dims:
        findings.append(
            _finding(
                "R-UNKNOWN-001",
                "risk",
                None,
                None,
                "显式 QoS 配置",
                "以下维度取值未知，无法完整确认兼容性：" + "；".join(unknown_dims),
            )
        )

    # --- R-DISC-001 端点当前处于发现宽限期（短暂未发现≠永久故障） ---
    missing = [e for e in (pub, sub) if e.state == "suspected_missing"]
    if missing:
        names = ", ".join(f"/{e.node_namespace.strip('/')}/{e.node_name}".replace("//", "/") for e in missing)
        findings.append(
            _finding(
                "R-DISC-001",
                "risk",
                pub.state,
                sub.state,
                "宽限期内保留最后已知状态，不判永久故障",
                f"端点 {names} 当前暂未被发现（在发现宽限期内），本结论基于其最后已知 QoS；"
                "等待重新发现或超过宽限期转为 lost，不视为永久故障。",
            )
        )

    if any(f.severity == "incompatible" for f in findings):
        verdict = "incompatible"
    elif any(f.severity == "risk" for f in findings):
        verdict = "risk"
    else:
        verdict = "compatible"
    return PairDiagnosis(publisher=pub, subscription=sub, verdict=verdict, findings=findings)  # type: ignore[arg-type]


def diagnose_topic(topic: str, endpoints: list[Endpoint]) -> TopicDiagnosis:
    """聚合一个话题上的全部活跃端点，做笛卡尔积配对诊断。"""
    pubs = [e for e in endpoints if e.topic == topic and e.endpoint_type == "publisher" and _active(e)]
    subs = [
        e for e in endpoints if e.topic == topic and e.endpoint_type == "subscription" and _active(e)
    ]
    all_topic_eps = [e for e in endpoints if e.topic == topic]
    topic_type = next((e.topic_type for e in all_topic_eps if e.topic_type), "")

    pairs = [evaluate_pair(p, s) for p in pubs for s in subs]
    matched_pub_ids = {id(pair.publisher) for pair in pairs}
    matched_sub_ids = {id(pair.subscription) for pair in pairs}
    unmatched_pubs = [p for p in pubs if id(p) not in matched_pub_ids]
    unmatched_subs = [s for s in subs if id(s) not in matched_sub_ids]

    if not pubs or not subs:
        # 端点未凑齐：证据不足，明确不臆断为故障
        verdict = "insufficient_evidence"
    elif any(pair.verdict == "incompatible" for pair in pairs):
        verdict = "incompatible"
    elif any(pair.verdict == "risk" for pair in pairs):
        verdict = "risk"
    else:
        verdict = "compatible"

    return TopicDiagnosis(
        topic=topic,
        topic_type=topic_type,
        publisher_count=len(pubs),
        subscription_count=len(subs),
        verdict=verdict,  # type: ignore[arg-type]
        pairs=pairs,
        unmatched_publishers=unmatched_pubs,
        unmatched_subscriptions=unmatched_subs,
    )


def diagnose_topology(topology: Topology) -> list[TopicDiagnosis]:
    topics = sorted({e.topic for e in topology.endpoints if _active(e)})
    return [diagnose_topic(t, list(topology.endpoints)) for t in topics]
