"""规则引擎离线测试（不需要 ROS）。"""

from __future__ import annotations

import json
from pathlib import Path

from qos_diag.models import Endpoint, QoSValues, Topology
from qos_diag.rules import RULES_VERSION, diagnose_topic, diagnose_topology, evaluate_pair

EXAMPLE = Path(__file__).resolve().parent.parent / "examples" / "sample_topology.json"


def make_ep(ep_type: str, *, reliability="reliable", durability="volatile",
            history="keep_last", depth=10, topic_type="std_msgs/msg/String",
            topic="/t", gid=None, state="discovered") -> Endpoint:
    return Endpoint(
        topic=topic,
        endpoint_type=ep_type,  # type: ignore[arg-type]
        node_name=f"{ep_type}_node",
        endpoint_gid=gid or f"gid-{ep_type}",
        topic_type=topic_type,
        qos=QoSValues(reliability=reliability, durability=durability, history=history, depth=depth),
        first_seen=0.0,
        last_seen=10.0,
        state=state,  # type: ignore[arg-type]
    )


def rule_of(pair, rule_id):
    hits = [f for f in pair.findings if f.rule_id == rule_id]
    return hits[0] if hits else None


# ---------------- reliability ----------------

def test_reliability_pub_be_sub_reliable_is_incompatible():
    pair = evaluate_pair(
        make_ep("publisher", reliability="best_effort", gid="p1"),
        make_ep("subscription", reliability="reliable", gid="s1"),
    )
    assert pair.verdict == "incompatible"
    f = rule_of(pair, "R-REL-001")
    assert f is not None and f.severity == "incompatible"
    # 匹配链路必须可解释：双方取值与期望值都在
    assert f.chain.publisher_value == "best_effort"
    assert f.chain.subscription_value == "reliable"
    assert "reliable" in f.chain.expected


def test_reliability_pub_reliable_sub_best_effort_is_compatible():
    pair = evaluate_pair(
        make_ep("publisher", reliability="reliable", gid="p1"),
        make_ep("subscription", reliability="best_effort", gid="s1"),
    )
    assert pair.verdict == "compatible"
    assert rule_of(pair, "R-REL-001").severity == "ok"


def test_both_reliable_compatible():
    pair = evaluate_pair(make_ep("publisher", gid="p1"), make_ep("subscription", gid="s1"))
    assert pair.verdict == "compatible"


# ---------------- durability ----------------

def test_durability_pub_volatile_sub_tl_is_incompatible():
    pair = evaluate_pair(
        make_ep("publisher", durability="volatile", gid="p1"),
        make_ep("subscription", durability="transient_local", gid="s1"),
    )
    assert pair.verdict == "incompatible"
    f = rule_of(pair, "R-DUR-001")
    assert f.severity == "incompatible"


def test_durability_pub_tl_sub_volatile_compatible():
    pair = evaluate_pair(
        make_ep("publisher", durability="transient_local", gid="p1"),
        make_ep("subscription", durability="volatile", gid="s1"),
    )
    assert rule_of(pair, "R-DUR-001").severity == "ok"


def test_both_transient_local_compatible():
    pair = evaluate_pair(
        make_ep("publisher", durability="transient_local", gid="p1"),
        make_ep("subscription", durability="transient_local", gid="s1"),
    )
    assert pair.verdict == "compatible"


# ---------------- history / depth 仅风险 ----------------

def test_history_mismatch_is_only_risk_not_incompatible():
    pair = evaluate_pair(
        make_ep("publisher", history="keep_all", depth=None, gid="p1"),
        make_ep("subscription", history="keep_last", depth=10, gid="s1"),
    )
    assert pair.verdict == "risk"
    assert rule_of(pair, "R-HIST-001").severity == "risk"
    assert not any(f.severity == "incompatible" for f in pair.findings)


def test_depth_mismatch_is_only_risk():
    pair = evaluate_pair(
        make_ep("publisher", depth=100, gid="p1"),
        make_ep("subscription", depth=10, gid="s1"),
    )
    assert pair.verdict == "risk"
    f = rule_of(pair, "R-DEPTH-001")
    assert f is not None and f.severity == "risk"


def test_matching_qos_no_findings_bad():
    pair = evaluate_pair(make_ep("publisher", depth=10, gid="p1"),
                         make_ep("subscription", depth=10, gid="s1"))
    assert pair.verdict == "compatible"
    assert all(f.severity == "ok" for f in pair.findings)


# ---------------- 类型不一致 ----------------

def test_type_mismatch_is_incompatible():
    pair = evaluate_pair(
        make_ep("publisher", topic_type="std_msgs/msg/String", gid="p1"),
        make_ep("subscription", topic_type="std_msgs/msg/Int32", gid="s1"),
    )
    assert rule_of(pair, "R-TYPE-001").severity == "incompatible"
    assert pair.verdict == "incompatible"


# ---------------- 话题级聚合：证据不足 ----------------

def test_topic_with_only_publisher_is_insufficient_evidence():
    diag = diagnose_topic("/lone", [make_ep("publisher", topic="/lone", gid="p1")])
    assert diag.verdict == "insufficient_evidence"
    assert len(diag.unmatched_publishers) == 1


def test_topic_empty_is_insufficient_evidence():
    diag = diagnose_topic("/none", [])
    assert diag.verdict == "insufficient_evidence"


def test_lost_endpoint_excluded_and_insufficient_evidence():
    """端点超过宽限期确认 lost 后退出配对，只剩一侧时必须是证据不足而非故障。"""
    lost_sub = make_ep("subscription", topic="/t", gid="s1", state="lost")
    diag = diagnose_topic("/t", [make_ep("publisher", topic="/t", gid="p1"), lost_sub])
    assert diag.verdict == "insufficient_evidence"
    assert diag.subscription_count == 0


def test_suspected_missing_is_risk_not_failure():
    """宽限期内暂未发现只能产生风险提示，不能判不兼容。"""
    pair = evaluate_pair(
        make_ep("publisher", gid="p1"),
        make_ep("subscription", gid="s1", state="suspected_missing"),
    )
    assert pair.verdict == "risk"
    disc = rule_of(pair, "R-DISC-001")
    assert disc is not None and disc.severity == "risk"


# ---------------- 示例文件端到端 ----------------

def test_sample_topology_results():
    topo = Topology.model_validate(json.loads(EXAMPLE.read_text()))
    diagnoses = {d.topic: d for d in diagnose_topology(topo)}

    assert diagnoses["/sensors/imu"].verdict == "incompatible"  # best_effort vs reliable
    assert diagnoses["/map"].verdict == "incompatible"          # volatile vs transient_local
    assert diagnoses["/telemetry"].verdict == "risk"            # depth 100 vs 10 仅风险
    assert diagnoses["/healthy_topic"].verdict == "compatible"
    assert diagnoses["/lone_publisher_topic"].verdict == "insufficient_evidence"

    # 风险话题绝不能出现 incompatible 级别 finding
    tele = diagnoses["/telemetry"]
    assert all(
        f.severity != "incompatible"
        for pair in tele.pairs for f in pair.findings
    )


def test_rules_version_semver_like():
    assert RULES_VERSION.startswith("qos-rules-")
