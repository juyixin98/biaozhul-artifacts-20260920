"""Gateway rclpy runtime.

One rclpy node per registered robot, created with the robot's namespace, so
that relative topic names expand to ``/<namespace>/cmd`` etc. structurally.
The HTTP layer never supplies a topic: the fixed topic allow-list in
``topics`` is all that is ever created.
"""

from __future__ import annotations

import threading
import time
import uuid
from typing import Any

from rclpy.callback_groups import ReentrantCallbackGroup
from rclpy.executors import MultiThreadedExecutor
from rclpy.node import Node
from std_msgs.msg import String

from .events import EventBus
from .protocol import (
    build_command_envelope,
    build_epoch_message,
    clock,
    decode_json,
    encode_json,
)
from .qos import LATCHED_QOS, VOLATILE_QOS
from .registry import RegistryStore
from .topics import CMD_TOPIC, EPOCH_TOPIC, EVENTS_TOPIC, STATUS_TOPIC

ONLINE_AFTER_SECONDS = 3.0
EPOCH_REPUBLISH_SECONDS = 2.0


class RobotWiring:
    def __init__(self, node: Node, robot_id: str, namespace: str) -> None:
        self.node = node
        self.robot_id = robot_id
        self.namespace = namespace
        self.cmd_pub = node.create_publisher(String, CMD_TOPIC, LATCHED_QOS)
        self.epoch_pub = node.create_publisher(String, EPOCH_TOPIC, LATCHED_QOS)
        node.create_subscription(String, STATUS_TOPIC, self._status_cb, VOLATILE_QOS)
        node.create_subscription(String, EVENTS_TOPIC, self._events_cb, VOLATILE_QOS)
        self.last_seq: dict[str, int] = {}

    def _status_cb(self, msg: String) -> None:
        try:
            payload = decode_json(msg.data)
        except ValueError:
            return
        if isinstance(payload, dict):
            self.bus_ref().status_snapshot(self.robot_id, payload)

    def _events_cb(self, msg: String) -> None:
        try:
            payload = decode_json(msg.data)
        except ValueError:
            return
        if isinstance(payload, dict):
            self.bus_ref().publish(payload)

    # attached by GatewayRuntime to avoid passing bus through constructors
    _runtime: "GatewayRuntime | None" = None

    def bus_ref(self) -> EventBus:
        assert self._runtime is not None
        return self._runtime.bus


class GatewayRuntime:
    def __init__(self, registry_path: str) -> None:
        import rclpy

        self._rclpy = rclpy
        self.store = RegistryStore(registry_path)
        self.bus = EventBus()
        self._wire_lock = threading.RLock()

        if not rclpy.ok():
            rclpy.init()
        self.group = ReentrantCallbackGroup()
        self.root = rclpy.create_node("p48_gateway")
        self.executor = MultiThreadedExecutor(num_threads=4)
        self.executor.add_node(self.root)

        self._wiring: dict[str, RobotWiring] = {}
        self._rewire_locked()
        self.epoch_timer = self.root.create_timer(
            EPOCH_REPUBLISH_SECONDS, self._republish_epochs, self.group
        )

        self._spin_thread = threading.Thread(
            target=self._spin, name="p48-gateway-spin", daemon=True
        )
        self._spin_thread.start()

    # -- wiring -------------------------------------------------------------
    def _make_wiring_locked(self, rid: str, namespace: str) -> RobotWiring:
        node = self._rclpy.create_node(f"p48_gw_{rid}", namespace=namespace)
        wiring = RobotWiring(node, rid, namespace)
        wiring._runtime = self
        self.executor.add_node(node)
        return wiring

    def _publish_epoch_locked(self, rid: str, wiring: RobotWiring, epoch: int) -> None:
        registry, _, _ = self.store.snapshot()
        msg = build_epoch_message(
            robot_id=rid,
            namespace=wiring.namespace,
            epoch=epoch,
            ts=clock(),
            epoch_key=registry.epoch_key,
        )
        wiring.epoch_pub.publish(String(data=encode_json(msg)))

    def _rewire_locked(self) -> None:
        registry, epoch, _ = self.store.snapshot()
        wanted = {rid: robot.namespace for rid, robot in registry.robots.items()}

        for rid in list(self._wiring):
            if rid not in wanted or self._wiring[rid].namespace != wanted[rid]:
                old = self._wiring.pop(rid)
                self.executor.remove_node(old.node)
                old.node.destroy_node()

        for rid, namespace in wanted.items():
            if rid not in self._wiring:
                wiring = self._make_wiring_locked(rid, namespace)
                self._wiring[rid] = wiring
                self._publish_epoch_locked(rid, wiring, epoch)

    def _republish_epochs(self) -> None:
        _, epoch, _ = self.store.snapshot()
        with self._wire_lock:
            for rid, wiring in list(self._wiring.items()):
                try:
                    self._publish_epoch_locked(rid, wiring, epoch)
                except Exception as exc:  # noqa: BLE001 - timer must not die
                    self.root.get_logger().warning(f"epoch publish failed for {rid}: {exc}")

    # -- commands -----------------------------------------------------------
    def dispatch_command(
        self, *, robot_id: str, tester_name: str, target: str, seq: int, command: dict,
        ttl_seconds: float | None,
    ) -> dict[str, Any]:
        """Validate and publish one command. Returns a result descriptor."""
        now = clock()
        registry, epoch, _ = self.store.snapshot()
        command_id = uuid.uuid4().hex
        base_event: dict[str, Any] = {
            "robot_id": robot_id,
            "tester": tester_name,
            "target": target,
            "seq": seq,
            "command_id": command_id,
            "epoch": epoch,
        }

        def reject(reason: str, status: int) -> dict[str, Any]:
            event = dict(base_event)
            event.update({"type": "ingress_rejected", "reason": reason, "ts": now})
            self.bus.publish(event)
            return {"accepted": False, "reason": reason, "http_status": status,
                    "command_id": command_id}

        robot = registry.robots.get(robot_id)
        if robot is None:
            # Unregistered namespace: the gateway can never address it.
            return reject("unknown_robot", 404)

        tester = registry.testers.get(tester_name)
        if tester is None or not registry.tester_can_access(tester, robot_id):
            return reject("not_authorized", 403)

        ttl = registry.default_ttl if ttl_seconds is None else ttl_seconds
        expires_at = now + float(ttl)
        if expires_at <= now:
            return reject("expired_at_ingress", 410)

        with self._wire_lock:
            wiring = self._wiring.get(robot_id)
            if wiring is None or wiring.namespace != robot.namespace:
                return reject("namespace_not_ready", 503)
            last = wiring.last_seq.get(target)
            if last is not None and seq <= last:
                return reject("stale_seq", 409)

            envelope = build_command_envelope(
                robot_id=robot_id,
                namespace=robot.namespace,
                target=target,
                seq=seq,
                command=command,
                expires_at=expires_at,
                issued_at=now,
                epoch=epoch,
                tester=tester_name,
                tester_key=tester.key,
                command_id=command_id,
            )
            wiring.cmd_pub.publish(String(data=encode_json(envelope)))
            wiring.last_seq[target] = seq

        event = dict(base_event)
        event.update({
            "type": "ingress_accepted",
            "topic": f"{robot.namespace}/{CMD_TOPIC}",
            "expires_at": expires_at,
            "ttl_seconds": ttl,
            "ts": now,
        })
        self.bus.publish(event)
        return {
            "accepted": True,
            "http_status": 202,
            "command_id": command_id,
            "robot_id": robot_id,
            "namespace": robot.namespace,
            "topic": f"{robot.namespace}/{CMD_TOPIC}",
            "seq": seq,
            "target": target,
            "epoch": epoch,
            "expires_at": expires_at,
        }

    # -- admin --------------------------------------------------------------
    def reload_registry(self) -> dict[str, Any]:
        with self._wire_lock:
            registry, epoch, changed = self.store.reload()
            if changed:
                self._rewire_locked()
                _, epoch, mapping = self.store.snapshot()
                for rid in mapping:
                    self.bus.publish({
                        "type": "mapping_changed",
                        "robot_id": rid,
                        "epoch": epoch,
                        "namespace": mapping[rid],
                        "ts": clock(),
                    })
                for rid, wiring in self._wiring.items():
                    self._publish_epoch_locked(rid, wiring, epoch)
            return {"changed": changed, "epoch": epoch,
                    "robots": sorted(registry.robots),
                    "testers": sorted(registry.testers)}

    # -- queries ------------------------------------------------------------
    def robot_status(self, robot_id: str) -> dict[str, Any] | None:
        registry, _, _ = self.store.snapshot()
        robot = registry.robots.get(robot_id)
        if robot is None:
            return None
        status = self.bus.get_status(robot_id)
        now = clock()
        if status is None:
            return {"robot_id": robot_id, "namespace": robot.namespace, "online": False,
                    "reason": "never_seen"}
        last_beat = float(status.get("ts", 0))
        status = dict(status)
        status["namespace"] = robot.namespace
        status["online"] = (now - last_beat) <= ONLINE_AFTER_SECONDS
        return status

    # -- lifecycle ----------------------------------------------------------
    def _spin(self) -> None:
        try:
            self.executor.spin()
        except Exception:  # noqa: BLE001 - shutdown races are expected
            pass

    def shutdown(self) -> None:
        with self._wire_lock:
            try:
                self.executor.shutdown(timeout_sec=1.0)
            except Exception:  # noqa: BLE001
                pass
            for wiring in self._wiring.values():
                wiring.node.destroy_node()
            self.root.destroy_node()
            if self._rclpy.ok():
                self._rclpy.shutdown()
