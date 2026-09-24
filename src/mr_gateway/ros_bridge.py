"""ROS 2 bridge: owns rclpy and publishes JSON command envelopes.

Threading
---------
rclpy must be initialized and spun from one thread. ``RosBridge`` runs a
dedicated daemon thread with its own executor; all node operations are
marshalled onto it through :meth:`_run` (a futures-based handoff), so the
asyncio request handlers never call rclpy directly.

Publishing
----------
One latched (``TRANSIENT_LOCAL``, reliable) publisher per fully-qualified
topic. Latching is what makes **reconnection** work: a synthetic robot node
that dies and respawns after a command was sent still receives the most
recent command upon rediscovering the publisher.

Only topics returned by :meth:`Registry.fqn` — i.e. absolute names strictly
under a *registered* namespace — are ever published. The bridge has no API to
publish an arbitrary topic name.
"""

from __future__ import annotations

import json
import queue
import threading
import time
from typing import Any

import rclpy
from rclpy.node import Node
from rclpy.executors import SingleThreadedExecutor
from rclpy.qos import QoSProfile, ReliabilityPolicy, DurabilityPolicy, HistoryPolicy
from std_msgs.msg import String

from .naming import NamingError

# Reliable + transient-local + keep-last => each new/late subscriber receives
# the last message automatically from DDS.
LATCHED_QOS = QoSProfile(
    reliability=ReliabilityPolicy.RELIABLE,
    durability=DurabilityPolicy.TRANSIENT_LOCAL,
    history=HistoryPolicy.KEEP_LAST,
    depth=10,
)


class RosBridge:
    def __init__(self, registry) -> None:
        self._registry = registry
        self._jobs: "queue.Queue[tuple]" = queue.Queue()
        self._node: Node | None = None
        self._publishers: dict[str, Any] = {}
        self._thread: threading.Thread | None = None
        self._ready = threading.Event()
        self._start_error: Exception | None = None
        # Private context: the gateway can init/shut rclpy independently of
        # other participants (synthetic test robots, other tools). They still
        # discover each other over DDS because they share the domain.
        self._ctx = None

    # ---- lifecycle --------------------------------------------------------

    def start(self) -> None:
        self._thread = threading.Thread(
            target=self._spin, name="mr-gateway-rclpy", daemon=True
        )
        self._thread.start()
        if not self._ready.wait(timeout=10):
            raise TimeoutError("rclpy bridge did not initialize in time")
        if self._start_error is not None:
            raise self._start_error

    def stop(self) -> None:
        self._jobs.put(("__stop__", None, None))
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None

    def _spin(self) -> None:
        executor = None
        try:
            self._ctx = rclpy.Context()
            self._ctx.init()
            self._node = Node("mr_gateway", context=self._ctx)
            executor = SingleThreadedExecutor(context=self._ctx)
            executor.add_node(self._node)
            self._ready.set()
        except Exception as exc:  # noqa: BLE001
            self._start_error = exc
            self._ready.set()
            return

        stopping = False
        pending: list[tuple] = []
        try:
            while not stopping:
                # Service DDS work (subscription matching etc.), then handle
                # any queued gateway jobs.
                executor.spin_once(timeout_sec=0.05)
                while True:
                    try:
                        kind, arg, fut = self._jobs.get_nowait()
                    except queue.Empty:
                        break
                    if kind == "__stop__":
                        stopping = True
                        break
                    try:
                        if kind == "publish":
                            result = self._do_publish(arg)
                        elif kind == "subscriber_count":
                            result = self._do_subscriber_count(arg)
                        elif kind == "dispose_namespace":
                            result = self._do_dispose_namespace(arg)
                        elif kind == "prewarm":
                            self._get_publisher(arg)
                            result = None
                        elif kind == "status":
                            result = self._do_status()
                        else:  # pragma: no cover - programming error
                            result = RuntimeError(f"unknown job {kind}")
                        if isinstance(result, Exception):
                            fut.set_exception(result)
                        else:
                            fut.set_result(result)
                    except Exception as exc:  # noqa: BLE001
                        fut.set_exception(exc)
        finally:
            if executor is not None:
                executor.shutdown()
            for pub in list(self._publishers.values()):
                self._node.destroy_publisher(pub)
            self._publishers.clear()
            self._node.destroy_node()
            try:
                self._ctx.shutdown()
            except Exception:  # noqa: BLE001
                pass

    def _run(self, kind: str, arg: Any = None, timeout: float = 10.0) -> Any:
        fut: "Future[Any]" = Future()
        self._jobs.put((kind, arg, fut))
        return fut.result(timeout=timeout)

    # ---- operations (executed on the rclpy thread) ------------------------

    def _get_publisher(self, fqn: str):
        pub = self._publishers.get(fqn)
        if pub is None:
            pub = self._node.create_publisher(String, fqn, LATCHED_QOS)
            self._publishers[fqn] = pub
        return pub

    def _do_publish(self, arg: tuple[str, dict[str, Any]]) -> dict[str, Any]:
        fqn, envelope = arg
        if not fqn.startswith("/"):  # belt-and-braces; naming.py already checks
            raise NamingError(f"refusing non-absolute FQN {fqn!r}")
        pub = self._get_publisher(fqn)
        msg = String()
        msg.data = json.dumps(envelope, separators=(",", ":"), sort_keys=True,
                              ensure_ascii=False)
        pub.publish(msg)
        return {
            "fqn": fqn,
            "published_at": time.time(),
            "subscriber_count": pub.get_subscription_count(),
        }

    def _do_subscriber_count(self, fqn: str) -> int:
        pub = self._publishers.get(fqn)
        return pub.get_subscription_count() if pub is not None else 0

    def _do_dispose_namespace(self, namespace: str) -> int:
        """Destroy publishers under a namespace after it is remapped/removed.

        The old namespace may belong to a different robot now; destroying our
        publishers guarantees we never emit into a namespace we no longer own.
        """
        prefix = "/" + namespace.strip("/") + "/"
        dead = [f for f in self._publishers if f.startswith(prefix)]
        for fqn in dead:
            self._node.destroy_publisher(self._publishers.pop(fqn))
        return len(dead)

    def _do_status(self) -> dict[str, Any]:
        return {
            "node": self._node.get_name(),
            "publishers": sorted(self._publishers.keys()),
        }

    # ---- public API (callable from any thread) ----------------------------

    def publish(self, fqn: str, envelope: dict[str, Any]) -> dict[str, Any]:
        return self._run("publish", (fqn, envelope))

    def subscriber_count(self, fqn: str) -> int:
        return self._run("subscriber_count", fqn)

    def dispose_namespace(self, namespace: str) -> int:
        return self._run("dispose_namespace", namespace)

    def status(self) -> dict[str, Any]:
        return self._run("status")

    def prewarm(self, fqn: str) -> None:
        """Create the publisher for a topic before a command is sent.

        A publisher created only when the first command races with DDS
        endpoint matching; warming up at registration makes discovery
        deterministic and lets subscriber counts be observed.
        """
        self._run("prewarm", fqn)


class Future:
    """Tiny thread-safe future to avoid concurrent.futures import noise."""

    def __init__(self) -> None:
        self._ev = threading.Event()
        self._result: Any = None
        self._exc: BaseException | None = None

    def set_result(self, value: Any) -> None:
        self._result = value
        self._ev.set()

    def set_exception(self, exc: BaseException) -> None:
        self._exc = exc
        self._ev.set()

    def result(self, timeout: float | None = None) -> Any:
        if not self._ev.wait(timeout):
            raise TimeoutError("bridge job timed out")
        if self._exc is not None:
            raise self._exc
        return self._result
