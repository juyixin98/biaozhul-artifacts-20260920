"""Synthetic robot node: real rclpy subscriber that enforces every rule.

Each robot lives in its registered namespace and subscribes to the relative
``cmd``/``epoch`` topics, so commands on a *same-named topic* in another
namespace never reach it.  On receipt it performs real HMAC verification,
mapping-epoch binding, target-sequence monotonicity, expiry and duplicate
delivery checks; every drop is reported on its own ``events`` topic with the
reason.  A 1 Hz ``status`` heartbeat drives the gateway online/offline view.
"""

from __future__ import annotations

import threading
import time
from collections import deque
from typing import Any

from rclpy.callback_groups import ReentrantCallbackGroup
from rclpy.executors import SingleThreadedExecutor
from rclpy.node import Node
from std_msgs.msg import String

from .crypto import canonical_json, hmac_verify
from .protocol import (
    clock,
    decode_json,
    encode_json,
    verify_command_envelope,
)
from .qos import LATCHED_QOS, VOLATILE_QOS
from .registry import Tester
from .topics import CMD_TOPIC, EPOCH_TOPIC, EVENTS_TOPIC, STATUS_TOPIC

STATUS_PERIOD_SECONDS = 1.0
SEEN_ID_LIMIT = 2000


class SyntheticRobot:
    def __init__(
        self,
        *,
        robot_id: str,
        namespace: str,
        epoch_key: str,
        testers: dict[str, Tester],
        heartbeat_seconds: float = STATUS_PERIOD_SECONDS,
    ) -> None:
        import rclpy

        self._rclpy = rclpy
        if not rclpy.ok():
            rclpy.init()
        self.robot_id = robot_id
        self.namespace = namespace
        self.epoch_key = epoch_key
        self.tester_keys = {name: t.key for name, t in testers.items()}

        self._lock = threading.Lock()
        self.current_epoch: int | None = None
        self.highest_seq: dict[str, int] = {}
        self._seen_deque: deque[str] = deque()
        self._seen: set[str] = set()
        self.applied: list[dict[str, Any]] = []
        self.last_event: dict[str, Any] | None = None
        self.epoch_events: list[dict[str, Any]] = []

        self.group = ReentrantCallbackGroup()
        self.node = rclpy.create_node(f"p48_robot_{robot_id}", namespace=namespace)
        self.status_pub = self.node.create_publisher(String, STATUS_TOPIC, VOLATILE_QOS)
        self.events_pub = self.node.create_publisher(String, EVENTS_TOPIC, VOLATILE_QOS)
        self.cmd_sub = self.node.create_subscription(
            String, CMD_TOPIC, self._on_command, LATCHED_QOS, callback_group=self.group
        )
        self.epoch_sub = self.node.create_subscription(
            String, EPOCH_TOPIC, self._on_epoch, LATCHED_QOS, callback_group=self.group
        )
        self.timer = self.node.create_timer(
            heartbeat_seconds, self._on_status, callback_group=self.group
        )

        self.executor = SingleThreadedExecutor()
        self.executor.add_node(self.node)
        self._thread = threading.Thread(
            target=self.executor.spin, name=f"robot-{robot_id}", daemon=True
        )
        self._thread.start()

    # -- helpers ------------------------------------------------------------
    def _emit(self, event_type: str, **fields: Any) -> dict[str, Any]:
        event = {"type": event_type, "robot_id": self.robot_id,
                 "namespace": self.namespace, "ts": clock(), **fields}
        with self._lock:
            self.last_event = event
            self.epoch_events.append(event) if event_type == "epoch_adopted" else None
        self.events_pub.publish(String(data=encode_json(event)))
        return event

    def _remember_id(self, command_id: str) -> None:
        if command_id in self._seen:
            return
        self._seen.add(command_id)
        self._seen_deque.append(command_id)
        if len(self._seen_deque) > SEEN_ID_LIMIT:
            old = self._seen_deque.popleft()
            self._seen.discard(old)

    # -- callbacks ----------------------------------------------------------
    def _on_epoch(self, msg: String) -> None:
        try:
            payload = decode_json(msg.data)
        except ValueError:
            self._emit("robot_rejected", reason="epoch_bad_json")
            return
        if not isinstance(payload, dict):
            self._emit("robot_rejected", reason="epoch_bad_shape")
            return
        signed = {k: v for k, v in payload.items() if k != "sig"}
        if not hmac_verify(canonical_json(signed), payload.get("sig", ""), self.epoch_key):
            self._emit("robot_rejected", reason="epoch_bad_signature")
            return
        if payload.get("robot_id") != self.robot_id:
            self._emit("robot_rejected", reason="epoch_robot_mismatch")
            return
        if payload.get("namespace") != self.namespace:
            self._emit("robot_rejected", reason="epoch_namespace_mismatch")
            return
        new_epoch = payload.get("epoch")
        if not isinstance(new_epoch, int) or new_epoch < 1:
            self._emit("robot_rejected", reason="epoch_bad_value")
            return
        with self._lock:
            old = self.current_epoch
            self.current_epoch = new_epoch
        if old != new_epoch:
            self._emit("epoch_adopted", old_epoch=old, epoch=new_epoch)

    def _on_command(self, msg: String) -> None:
        now = clock()
        try:
            envelope = decode_json(msg.data)
        except ValueError:
            self._emit("robot_rejected", reason="malformed_json")
            return

        with self._lock:
            current_epoch = self.current_epoch

        if current_epoch is None:
            self._emit("robot_rejected", reason="epoch_not_learned",
                       target=envelope.get("target") if isinstance(envelope, dict) else None)
            return

        ok, reason = verify_command_envelope(
            envelope,
            expected_robot_id=self.robot_id,
            expected_namespace=self.namespace,
            current_epoch=current_epoch,
            tester_keys=self.tester_keys,
            now=now,
            seen_ids=self._seen,
        )
        if not ok:
            self._emit(
                "robot_rejected",
                reason=reason,
                target=envelope.get("target") if isinstance(envelope, dict) else None,
                seq=envelope.get("seq") if isinstance(envelope, dict) else None,
            )
            return

        with self._lock:
            last = self.highest_seq.get(envelope["target"])
            if last is not None and envelope["seq"] <= last:
                self._emit("robot_rejected", reason="stale_seq_at_robot",
                           target=envelope["target"], seq=envelope["seq"],
                           command_id=envelope["id"])
                return
            self.highest_seq[envelope["target"]] = envelope["seq"]
            self._remember_id(envelope["id"])
            self.applied.append({
                "command_id": envelope["id"],
                "target": envelope["target"],
                "seq": envelope["seq"],
                "command": envelope["command"],
                "tester": envelope["tester"],
                "epoch": envelope["epoch"],
                "applied_at": now,
            })
        self._emit(
            "command_applied",
            command_id=envelope["id"],
            target=envelope["target"],
            seq=envelope["seq"],
            tester=envelope["tester"],
        )

    def _on_status(self) -> None:
        with self._lock:
            status = {
                "robot_id": self.robot_id,
                "namespace": self.namespace,
                "epoch": self.current_epoch,
                "ts": clock(),
                "applied_count": len(self.applied),
                "last_target": self.applied[-1]["target"] if self.applied else None,
                "uptime_note": "synthetic robot heartbeat",
            }
        self.status_pub.publish(String(data=encode_json(status)))

    # -- test helpers -------------------------------------------------------
    def wait_until(self, predicate, timeout: float = 5.0) -> bool:
        deadline = time.time() + timeout
        while time.time() < deadline:
            with self._lock:
                if predicate(self):
                    return True
            time.sleep(0.02)
        with self._lock:
            return predicate(self)

    def drop_command_subscription(self) -> None:
        """Simulate losing the command link (node keeps running)."""
        with self._lock:
            self.node.destroy_subscription(self.cmd_sub)
        self.cmd_sub = None

    def restore_command_subscription(self) -> None:
        """Reconnect: new subscription to the same latched topic, state kept."""
        if self.cmd_sub is not None:
            return
        self.cmd_sub = self.node.create_subscription(
            String, CMD_TOPIC, self._on_command, LATCHED_QOS, callback_group=self.group
        )

    def stop(self) -> None:
        try:
            self.executor.shutdown(timeout_sec=1.0)
        except Exception:  # noqa: BLE001
            pass
        self.node.destroy_node()
