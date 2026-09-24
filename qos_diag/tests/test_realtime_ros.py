"""真实 ROS 2 集成测试：在同一进程里创建实际 DDS 节点 + 采集器。

与单元测试的区别：端点信息来自 Fast DDS 的真实发现（Graph API），
不是手工构造的对象。没有 ROS 环境时整个文件 skip。
"""

from __future__ import annotations

import os
import random
import time

import pytest

rclpy = pytest.importorskip("rclpy", reason="需要 source ROS 2 后才能运行真实发现测试")
# ROS_DOMAIN_ID 必须在 rclpy 初始化前确定
os.environ.setdefault("ROS_DOMAIN_ID", str(random.randint(100, 160)))

from rclpy.node import Node  # noqa: E402
from rclpy.qos import DurabilityPolicy, HistoryPolicy, QoSProfile, ReliabilityPolicy  # noqa: E402
from std_msgs.msg import String  # noqa: E402

from qos_diag.collector import QoSTopologyCollector  # noqa: E402
from qos_diag.rules import diagnose_topic  # noqa: E402


@pytest.fixture(scope="module")
def ros_env():
    rclpy.init()
    collector = QoSTopologyCollector(
        discovery_interval=0.3, missing_grace_seconds=1.5,
        node_name="qos_diag_test_collector",
    )
    collector.start()
    nodes = []
    try:
        yield collector, nodes
    finally:
        for n in nodes:
            n.destroy_node()
        collector.shutdown()
        if rclpy.ok():
            rclpy.shutdown()


def qos(reliability, durability, history=HistoryPolicy.KEEP_LAST, depth=10):
    # Jazzy 要求 KEEP_LAST 在构造时就带 depth
    return QoSProfile(
        reliability=reliability, durability=durability, history=history, depth=depth,
    )


def wait_topic(collector, topic, n_pub, n_sub, timeout=10.0):
    assert collector.wait_for_endpoints(topic, n_pub, n_sub, timeout), (
        f"真实 DDS 发现超时：topic={topic} 期望 pub={n_pub} sub={n_sub}"
    )


def make_node(ros_env, name):
    collector, nodes = ros_env
    node = rclpy.create_node(name)
    nodes.append(node)
    return collector, node


def _topic_diag(collector, topic):
    topo = collector.get_topology()
    diags = [d for d in [diagnose_topic(topic, list(topo.endpoints))]]
    return diags[0]


def _no_incompatible(diag):
    return not any(
        f.severity == "incompatible" for pair in diag.pairs for f in pair.findings
    )


def _rule(diag, rule_id, severity=None):
    for pair in diag.pairs:
        for f in pair.findings:
            if f.rule_id == rule_id and (severity is None or f.severity == severity):
                return f
    return None


def test_realtime_reliability_incompatibility_and_recovery(ros_env):
    """真实 best_effort 发布 + reliable 订阅 => 发现层回报不兼容；修正后恢复。"""
    topic = f"/qos_it/reliability_{os.environ['ROS_DOMAIN_ID']}"
    collector, pub_node = make_node(ros_env, "it_pub_node")
    _, sub_node = make_node(ros_env, "it_sub_node")

    sub = sub_node.create_subscription(
        String, topic, lambda _m: None,
        qos(ReliabilityPolicy.RELIABLE, DurabilityPolicy.VOLATILE),
    )
    pub = pub_node.create_publisher(
        String, topic, qos(ReliabilityPolicy.BEST_EFFORT, DurabilityPolicy.VOLATILE),
    )
    wait_topic(collector, topic, 1, 1)
    time.sleep(0.6)  # 让采集器至少跑两轮

    diag = _topic_diag(collector, topic)
    assert diag.verdict == "incompatible"
    assert _rule(diag, "R-REL-001", "incompatible") is not None

    # 端点发现事件带真实时间戳
    topo = collector.get_topology()
    evs = [e for e in topo.events if e.topic == topic and e.event == "discovered"]
    assert len(evs) >= 2
    assert all(e.timestamp > 0 for e in evs)

    # 修正：销毁 best_effort publisher，创建 reliable publisher
    pub_node.destroy_publisher(pub)
    time.sleep(0.5)
    pub2 = pub_node.create_publisher(
        String, topic, qos(ReliabilityPolicy.RELIABLE, DurabilityPolicy.VOLATILE),
    )
    deadline = time.time() + 10
    while time.time() < deadline:
        diag = _topic_diag(collector, topic)
        if _no_incompatible(diag) and _rule(diag, "R-REL-001", "ok"):
            break
        time.sleep(0.3)
    else:
        raise AssertionError("修正后可靠性维度未恢复通过")
    # 真实 DDS 发现不携带 history/depth（回报 unknown/0），因此整体可能是 risk(R-UNKNOWN-001)，
    # 但不能再有任何 incompatible
    assert diag.verdict in ("compatible", "risk")
    pub_node.destroy_publisher(pub2)
    sub_node.destroy_subscription(sub)


def test_realtime_durability_incompatibility(ros_env):
    topic = f"/qos_it/durability_{os.environ['ROS_DOMAIN_ID']}"
    collector, pub_node = make_node(ros_env, "it_pub_node_d")
    _, sub_node = make_node(ros_env, "it_sub_node_d")

    sub = sub_node.create_subscription(
        String, topic, lambda _m: None,
        qos(ReliabilityPolicy.RELIABLE, DurabilityPolicy.TRANSIENT_LOCAL),
    )
    pub = pub_node.create_publisher(
        String, topic, qos(ReliabilityPolicy.RELIABLE, DurabilityPolicy.VOLATILE),
    )
    wait_topic(collector, topic, 1, 1)
    time.sleep(0.6)

    diag = _topic_diag(collector, topic)
    assert diag.verdict == "incompatible"
    assert any(
        f.rule_id == "R-DUR-001" and f.severity == "incompatible"
        for pair in diag.pairs for f in pair.findings
    )

    # 物理证据：真实 DDS 下该订阅不应收到任何消息
    received = []
    sub_node.destroy_subscription(sub)
    sub2 = sub_node.create_subscription(
        String, topic, lambda m: received.append(m),
        qos(ReliabilityPolicy.RELIABLE, DurabilityPolicy.TRANSIENT_LOCAL),
    )
    end = time.time() + 2.5
    while time.time() < end:
        rclpy.spin_once(sub_node, timeout_sec=0.1)
        rclpy.spin_once(pub_node, timeout_sec=0.0)
        pub.publish(String())
    assert received == [], "volatile pub + transient_local sub 链路不应收到消息"

    pub_node.destroy_publisher(pub)
    sub_node.destroy_subscription(sub2)


def test_realtime_history_depth_not_observable_on_wire(ros_env):
    """真实 DDS（Fast DDS）发现层不传播 history/depth：回报 unknown/0。

    这意味着：不同 depth(10/100) 的两端在实时诊断里绝不能被判 incompatible，
    而应给出 R-UNKNOWN-001 风险提示，要求显式确认该维度。
    history/depth 的“仅风险”规则本身由 tests/test_rules.py 离线覆盖。
    """
    topic = f"/qos_it/risk_{os.environ['ROS_DOMAIN_ID']}"
    collector, pub_node = make_node(ros_env, "it_pub_node_r")
    _, sub_node = make_node(ros_env, "it_sub_node_r")

    sub = sub_node.create_subscription(
        String, topic, lambda _m: None,
        qos(ReliabilityPolicy.RELIABLE, DurabilityPolicy.VOLATILE,
            history=HistoryPolicy.KEEP_LAST, depth=10),
    )
    pub = pub_node.create_publisher(
        String, topic,
        qos(ReliabilityPolicy.RELIABLE, DurabilityPolicy.VOLATILE,
            history=HistoryPolicy.KEEP_LAST, depth=100),
    )
    wait_topic(collector, topic, 1, 1)
    time.sleep(0.6)

    topo = collector.get_topology()
    eps = [e for e in topo.endpoints if e.topic == topic]
    # 真实回报：history unknown / depth 0
    assert all(e.qos.history in ("unknown", "system_default") for e in eps)

    diag = _topic_diag(collector, topic)
    assert diag.verdict == "risk"
    assert not any(
        f.severity == "incompatible"
        for pair in diag.pairs for f in pair.findings
    )
    assert _rule(diag, "R-UNKNOWN-001") is not None

    # 物理证据：即使 depth 不同，链路是真实连通可收发的
    got = []
    sub_node.destroy_subscription(sub)
    sub2 = sub_node.create_subscription(
        String, topic, lambda m: got.append(m),
        qos(ReliabilityPolicy.RELIABLE, DurabilityPolicy.VOLATILE,
            history=HistoryPolicy.KEEP_LAST, depth=10),
    )
    end = time.time() + 2.0
    while time.time() < end:
        rclpy.spin_once(sub_node, timeout_sec=0.05)
        rclpy.spin_once(pub_node, timeout_sec=0.0)
        pub.publish(String())
    assert len(got) > 0, "reliability/durability 兼容时 depth 差异不应阻止收发"

    pub_node.destroy_publisher(pub)
    sub_node.destroy_subscription(sub2)


def test_realtime_endpoint_lifecycle_grace_period(ros_env):
    """端点生命周期，两种真实情形：

    1) 短暂未发现（同一 GID，宽限期内重新出现）=> suspected_missing 后 rediscovered；
    2) DDS 实体重建会分配新 GID => 旧端点超宽限转 lost，新端点全新 discovered。
    所有状态转换都带真实时间戳，短暂未发现绝不立即判永久故障。
    """
    # ---- 情形1：不销毁实体，用假时钟模拟“同一 GID 短暂未发现” ----
    from qos_diag.models import Endpoint, QoSValues

    fake_clock = QoSTopologyCollector(
        discovery_interval=0.3, missing_grace_seconds=1.5,
        node_name="qos_diag_clock_coll", time_func=lambda: 100.0,
    )
    fake_clock._owns_rclpy = False  # rclpy 由模块 fixture 统一管理
    # 直接用内部状态机构造：同一 key 先 observed，再两轮未 observed（不超宽限），再 observed
    gid_hex = bytes(b"\x01\x02gid-xyz").hex()
    info_ep = Endpoint(
        topic="/clock_topic", endpoint_type="publisher", node_name="cn",
        endpoint_gid=gid_hex, topic_type="std_msgs/msg/String",
        qos=QoSValues(reliability="reliable", durability="volatile",
                      history="keep_last", depth=10),
        first_seen=100.0, last_seen=100.0,
    )
    fake_clock._endpoints[info_ep.key] = info_ep
    fake_clock._reap(100.5, set())  # 本轮未发现
    assert fake_clock._endpoints[info_ep.key].state == "suspected_missing"
    fake_clock._reap(101.0, set())  # 仍未发现，但 1.0s < 1.5s 宽限
    assert fake_clock._endpoints[info_ep.key].state == "suspected_missing"
    # 同一 GID 重新出现 => rediscovered
    class _Info:
        endpoint_type = None
        node_name = "cn"; node_namespace = "/"
        endpoint_gid = gid_hex  # 与手工 Endpoint 保持一致（str 走 _gid_hex 的 str 分支）
        topic_type = "std_msgs/msg/String"
        class qos_profile:
            reliability = 1; durability = 2; history = 1; depth = 10
    from rclpy.topic_endpoint_info import TopicEndpointTypeEnum
    _Info.endpoint_type = TopicEndpointTypeEnum.PUBLISHER
    fake_clock._observe(_Info(), "/clock_topic", "publisher", 101.2)
    ep = fake_clock._endpoints[info_ep.key]
    assert ep.state == "discovered" and ep.missing_since is None
    assert any(
        e.event == "rediscovered" and e.timestamp == 101.2
        for e in fake_clock._events
    )
    # 超过宽限期仍未出现 => lost
    fake_clock._reap(102.0, set())
    fake_clock._age_suspected(103.6)
    assert fake_clock._endpoints[info_ep.key].state == "lost"
    assert any(e.event == "lost" for e in fake_clock._events)
    fake_clock.shutdown()

    # ---- 情形2：真实 DDS 销毁/重建，GID 变化 ----
    topic = f"/qos_it/life_{os.environ['ROS_DOMAIN_ID']}"
    collector, pub_node = make_node(ros_env, "it_life_node")

    pub = pub_node.create_publisher(
        String, topic, qos(ReliabilityPolicy.RELIABLE, DurabilityPolicy.VOLATILE),
    )
    assert collector.wait_for_endpoints(topic, 1, 0, timeout=10.0)
    time.sleep(0.6)
    gid_before = next(e.endpoint_gid for e in collector.get_topology().endpoints
                      if e.topic == topic and e.state == "discovered")
    assert gid_before

    pub_node.destroy_publisher(pub)
    time.sleep(0.7)  # 宽限期 1.5s 内
    ep = next(e for e in collector.get_topology().endpoints if e.topic == topic)
    assert ep.state == "suspected_missing", "短暂未发现不应立即判故障"
    assert ep.missing_since is not None

    pub2 = pub_node.create_publisher(
        String, topic, qos(ReliabilityPolicy.RELIABLE, DurabilityPolicy.VOLATILE),
    )
    # 新实体带新 GID：出现一个 discovered 新端点；旧 GID 超宽限后 lost
    deadline = time.time() + 8
    new_gid = None
    while time.time() < deadline:
        eps = [e for e in collector.get_topology().endpoints if e.topic == topic]
        disc = [e for e in eps if e.state == "discovered"]
        if disc and disc[0].endpoint_gid != gid_before:
            new_gid = disc[0].endpoint_gid
            break
        time.sleep(0.2)
    assert new_gid, "重建后应发现带新 GID 的端点"
    events = collector.get_topology().events
    assert any(e.event == "discovered" and e.timestamp > 0 and e.topic == topic for e in events)

    # 等旧 GID 宽限到期
    time.sleep(1.6)
    topo_end = collector.get_topology()
    states = {e.endpoint_gid: e.state for e in topo_end.endpoints
              if e.topic == topic}
    assert states.get(gid_before) == "lost"
    assert states.get(new_gid) == "discovered"
    assert any(e.event == "lost" and e.timestamp > 0 for e in topo_end.events if e.topic == topic)
    pub_node.destroy_publisher(pub2)
