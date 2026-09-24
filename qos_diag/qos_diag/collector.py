"""基于 rclpy 的真实 DDS 端点发现与生命周期跟踪。

工作方式：后台线程周期性调用 ROS Graph API
（get_topic_names_and_types / get_publishers_info_by_topic /
get_subscriptions_info_by_topic），这些接口直接读取 DDS 发现层
（Fast DDS / rmw_fastrtps_cpp）的真实端点信息，不做任何静态假设。

端点生命周期（带时间戳）：

    (new) discovered ──本轮未发现──▶ suspected_missing（保留 last-known，宽限期）
                       ◀─重新发现──        │ 超过 missing_grace_seconds
                       rediscovered        ▼
                                          lost（确认消失，发出 lost 事件）

关键原则：端点在宽限期内未被观察到只会进入 suspected_missing，绝不立即判永久故障；
重新发现时发出 rediscovered 事件并恢复诊断，超过宽限期才转为 lost。
"""

from __future__ import annotations

import threading
import time
from collections import deque
from typing import Callable

from .models import Endpoint, EndpointEvent, QoSValues, Topology

try:  # pragma: no cover - 导入分支由有无 ROS 环境决定
    import rclpy
    from rclpy.node import Node
    from rclpy.qos import (
        DurabilityPolicy,
        HistoryPolicy,
        ReliabilityPolicy,
    )
    from rclpy.topic_endpoint_info import TopicEndpointTypeEnum

    _RCLPY_AVAILABLE = True
except ImportError:  # pragma: no cover
    _RCLPY_AVAILABLE = False


def require_rclpy() -> None:
    if not _RCLPY_AVAILABLE:
        raise RuntimeError(
            "rclpy 不可用：请先 source ROS 2（例如 `source /opt/ros/jazzy/setup.bash`）"
        )


def _enum_name(value: int, enum_cls: type) -> str:
    try:
        return enum_cls(value).name.lower()
    except ValueError:
        return "unknown"


def _gid_hex(gid: object) -> str:
    if gid is None:
        return ""
    try:
        return bytes(gid).hex()  # type: ignore[arg-type]
    except TypeError:
        return str(gid)


def qos_profile_to_values(qos: object) -> QoSValues:
    return QoSValues(
        reliability=_enum_name(int(qos.reliability), ReliabilityPolicy),  # type: ignore[attr-defined]
        durability=_enum_name(int(qos.durability), DurabilityPolicy),  # type: ignore[attr-defined]
        history=_enum_name(int(qos.history), HistoryPolicy),  # type: ignore[attr-defined]
        depth=int(qos.depth),  # type: ignore[attr-defined]
    )


class QoSTopologyCollector:
    def __init__(
        self,
        discovery_interval: float = 1.0,
        missing_grace_seconds: float = 5.0,
        time_func: Callable[[], float] = time.time,
        node_name: str = "qos_diag_collector",
    ) -> None:
        require_rclpy()
        self.discovery_interval = discovery_interval
        self.missing_grace_seconds = missing_grace_seconds
        self._time = time_func
        self._lock = threading.RLock()
        self._endpoints: dict[str, Endpoint] = {}
        self._events: deque[EndpointEvent] = deque(maxlen=2000)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._owns_rclpy = False

        if not rclpy.ok():
            rclpy.init()
            self._owns_rclpy = True
        self._node: Node = rclpy.create_node(node_name)
        # 私有 executor：避免与宿主进程里其他节点的全局 executor 自旋冲突
        from rclpy.executors import SingleThreadedExecutor

        self._executor = SingleThreadedExecutor()
        self._executor.add_node(self._node)

    # ------------------------------------------------------------------
    def _make_endpoint(self, info: object, topic: str, ep_type: str, now: float) -> Endpoint:
        return Endpoint(
            topic=topic,
            endpoint_type=ep_type,  # type: ignore[arg-type]
            node_name=str(info.node_name),  # type: ignore[attr-defined]
            node_namespace=str(info.node_namespace) or "/",  # type: ignore[attr-defined]
            endpoint_gid=_gid_hex(info.endpoint_gid),  # type: ignore[attr-defined]
            topic_type=str(info.topic_type),  # type: ignore[attr-defined]
            qos=qos_profile_to_values(info.qos_profile),  # type: ignore[attr-defined]
            first_seen=now,
            last_seen=now,
        )

    def _emit(self, event_type: str, ep: Endpoint, now: float) -> None:
        self._events.append(
            EndpointEvent(
                endpoint_key=ep.key,
                topic=ep.topic,
                event=event_type,  # type: ignore[arg-type]
                timestamp=now,
                endpoint=ep.model_copy(),
            )
        )

    def scan_once(self) -> None:
        """执行一轮真实图发现扫描，并推进端点生命周期。"""
        now = self._time()
        seen: set[str] = set()
        with self._lock:
            for topic, _types in self._node.get_topic_names_and_types():
                for info in self._node.get_publishers_info_by_topic(topic):
                    if info.endpoint_type == TopicEndpointTypeEnum.PUBLISHER:
                        seen.add(self._observe(info, topic, "publisher", now))
                for info in self._node.get_subscriptions_info_by_topic(topic):
                    if info.endpoint_type == TopicEndpointTypeEnum.SUBSCRIPTION:
                        seen.add(self._observe(info, topic, "subscription", now))
            self._reap(now, seen)

    def _observe(self, info: object, topic: str, ep_type: str, now: float) -> str:
        ep = self._make_endpoint(info, topic, ep_type, now)
        existing = self._endpoints.get(ep.key)
        if existing is None:
            self._endpoints[ep.key] = ep
            self._emit("discovered", ep, now)
            return ep.key
        # 更新 last-known 信息（QoS 可能在重建后变化；GID 相同则实体仍在）
        updated = existing.model_copy(
            update={
                "last_seen": now,
                "qos": ep.qos,
                "topic_type": ep.topic_type or existing.topic_type,
            }
        )
        if existing.state == "suspected_missing":
            updated = updated.model_copy(
                update={"state": "discovered", "missing_since": None}
            )
            self._endpoints[ep.key] = updated
            self._emit("rediscovered", updated, now)
        else:
            self._endpoints[ep.key] = updated
        return ep.key

    def _reap(self, now: float, seen: set[str]) -> None:
        """处理本轮未发现端点：先 suspected_missing（宽限），超时才 lost。"""
        for key, ep in list(self._endpoints.items()):
            if key in seen or ep.state == "lost":
                continue
            if ep.state == "discovered":
                # 立即标记“暂未发现”，但保留最后已知 QoS，等待宽限 —— 不当作故障
                self._endpoints[key] = ep.model_copy(
                    update={"state": "suspected_missing", "missing_since": now}
                )
        self._age_suspected(now)

    def _age_suspected(self, now: float) -> None:
        """只推进“暂未发现”端点的宽限期计时，不影响其他端点。"""
        for key, ep in list(self._endpoints.items()):
            if ep.state == "suspected_missing" and ep.missing_since is not None:
                if now - ep.missing_since >= self.missing_grace_seconds:
                    lost = ep.model_copy(update={"state": "lost"})
                    self._endpoints[key] = lost
                    self._emit("lost", lost, now)

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self._executor.spin_once(timeout_sec=0.1)
                self.scan_once()
            except Exception:  # pragma: no cover - 后台循环不能因一次扫描异常退出
                pass
            self._stop.wait(self.discovery_interval)

    def start(self) -> None:
        if self._thread is not None:
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, name="qos-diag-collector", daemon=True
        )
        self._thread.start()

    def shutdown(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=3.0)
            self._thread = None
        try:
            self._executor.shutdown(timeout_sec=1.0)
            self._node.destroy_node()
        finally:
            if self._owns_rclpy and rclpy.ok():
                rclpy.shutdown()

    # ------------------------------------------------------------------
    def get_topology(self) -> Topology:
        with self._lock:
            # 取出前按当前时间推进一次宽限期（即使没有新扫描，suspected 也能到期转 lost）
            self._age_suspected(self._time())
            return Topology(
                captured_at=self._time(),
                discovery_interval=self.discovery_interval,
                missing_grace_seconds=self.missing_grace_seconds,
                endpoints=[e.model_copy(deep=True) for e in self._endpoints.values()],
                events=list(self._events),
            )

    def wait_for_endpoints(
        self,
        topic: str,
        min_publishers: int = 0,
        min_subscriptions: int = 0,
        timeout: float = 15.0,
    ) -> bool:
        """阻塞等待某话题出现指定数量的活跃端点（真实发现，供测试/验收使用）。"""
        deadline = self._time() + timeout
        while self._time() < deadline:
            topo = self.get_topology()
            pubs = [e for e in topo.endpoints if e.topic == topic and e.endpoint_type == "publisher"]
            subs = [
                e for e in topo.endpoints if e.topic == topic and e.endpoint_type == "subscription"
            ]
            if len(pubs) >= min_publishers and len(subs) >= min_subscriptions:
                return True
            time.sleep(0.2)
        return False
