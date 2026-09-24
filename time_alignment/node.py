"""rclpy alignment node.

Importing this module does NOT require rclpy: all ROS symbols are imported
lazily inside :func:`create_aligner_node`, and the message->StreamEvent
conversion lives in pure functions that accept duck-typed messages.  That
lets the test-suite drive the real node callbacks with lightweight fake
messages on machines without ROS installed.

ROS parameters (declare_parameters / `ros2 param set`):
    tolerance_ns, reorder_tolerance_ns, imu_exclusive, imu_max_uses,
    max_camera_pending, max_imu_pending
Updates are queued on the live executor and applied atomically at the next
receive boundary; every emitted evidence row records the parameter version.
"""
from __future__ import annotations

import os
import secrets
from pathlib import Path
from typing import Any, Optional

from .matcher import AlignmentMatcher
from .model import AlignParams, StreamEvent
from .storage import EvidenceStore

CAMERA_TOPIC = "camera/image"
IMU_TOPIC = "imu/data"
RESET_TOPIC = "clock/reset"

_ROS_PARAM_MAP = {
    "tolerance_ns": "tolerance_ns",
    "reorder_tolerance_ns": "reorder_tolerance_ns",
    "imu_exclusive": "imu_exclusive",
    "imu_max_uses": "imu_max_uses",
    "max_camera_pending": "max_camera_pending",
    "max_imu_pending": "max_imu_pending",
}


def ros_time_to_ns(stamp: Any) -> int:
    """builtin_interfaces/Time (sec,nanosec) -> integer nanoseconds."""
    return int(stamp.sec) * 1_000_000_000 + int(stamp.nanosec)


def camera_to_event(msg: Any, recv_ns: Optional[int] = None) -> StreamEvent:
    """sensor_msgs/Image or any header-stamped camera message."""
    src = getattr(msg, "header", msg)
    frame_id = getattr(src, "frame_id", "")
    stamp = getattr(src, "stamp", None)
    source_id = f"{frame_id}:{ros_time_to_ns(stamp)}"
    return StreamEvent(
        "camera", ros_time_to_ns(stamp), source_id=source_id,
        recv_ns=recv_ns,
        payload={"frame_id": frame_id,
                 "width": getattr(msg, "width", None),
                 "height": getattr(msg, "height", None)})


def imu_to_event(msg: Any, recv_ns: Optional[int] = None) -> StreamEvent:
    """sensor_msgs/Imu."""
    stamp = msg.header.stamp
    frame_id = getattr(msg.header, "frame_id", "")
    # sequence is not in ROS2 headers; identity = frame_id + stamp; the
    # matcher assigns a monotonic receive seq on top.
    source_id = f"{frame_id}:{ros_time_to_ns(stamp)}"
    return StreamEvent(
        "imu", ros_time_to_ns(stamp), source_id=source_id,
        recv_ns=recv_ns,
        payload={"frame_id": frame_id,
                 "ax": getattr(msg.linear_acceleration, "x", None)})


def split_source_id(source_id: str) -> tuple[str, Optional[int]]:
    if ":" in source_id:
        fid, ts = source_id.rsplit(":", 1)
        try:
            return fid, int(ts)
        except ValueError:
            pass
    return source_id, None


class AlignerCore:
    """Node-independent wiring: matcher + evidence store + param handling.

    Used directly by the rclpy node and by tests with fake messages.
    """

    def __init__(self, params: AlignParams, store: EvidenceStore,
                 clock_ns=None, epoch_hint_fn=None, clock_gen_fn=None) -> None:
        self.matcher = AlignmentMatcher(params)
        self.store = store
        self._clock_ns = clock_ns  # zero-arg callable returning receive ns
        self._epoch_hint_fn = epoch_hint_fn
        self._clock_gen_fn = clock_gen_fn
        self._param_version = params.version

    def _recv(self) -> Optional[int]:
        if self._clock_ns is None:
            return None
        try:
            return int(self._clock_ns())
        except Exception:
            return None

    def _hint(self) -> Optional[str]:
        if self._epoch_hint_fn is None:
            return None
        try:
            return str(self._epoch_hint_fn())
        except Exception:
            return None

    def _generation(self) -> int:
        if self._clock_gen_fn is None:
            return 0
        try:
            return int(self._clock_gen_fn())
        except Exception:
            return 0

    def handle_camera(self, msg: Any) -> list[dict]:
        ev = camera_to_event(msg, self._recv())
        if ev.epoch_hint is None:
            object.__setattr__(ev, "epoch_hint", self._hint())
        object.__setattr__(ev, "clock_generation", self._generation())
        return self._commit(self.matcher.register(ev))

    def handle_imu(self, msg: Any) -> list[dict]:
        ev = imu_to_event(msg, self._recv())
        if ev.epoch_hint is None:
            object.__setattr__(ev, "epoch_hint", self._hint())
        object.__setattr__(ev, "clock_generation", self._generation())
        return self._commit(self.matcher.register(ev))

    def handle_reset(self, source_id: str = "external_reset",
                     detail: Optional[dict] = None) -> list[dict]:
        ev = StreamEvent("reset", None, source_id=source_id,
                         payload=detail or {})
        return self._commit(self.matcher.register(ev))

    def update_params(self, **new_values: Any) -> int:
        """Validate and queue a new parameter version; returns its version."""
        self._param_version += 1
        current = self.matcher.params.to_dict()
        current.update(new_values)
        current["version"] = self._param_version
        p = AlignParams.from_dict(current)
        self.matcher.request_params(p)  # raises immediately on invalid values
        return self._param_version

    def finalize(self) -> list[dict]:
        return self._commit(self.matcher.finalize())

    def _commit(self, result) -> list[dict]:
        recs = self.store.append_many(result.outcomes)
        return [
            {"seq": r.seq, "kind": r.kind.value, "epoch": r.epoch,
             "params_version": r.params_version, **r.payload}
            for r in recs]


def create_aligner_node(
        *, db_path: str, key: Optional[bytes] = None,
        key_file: Optional[str] = None,
        overrides: Optional[dict[str, Any]] = None):
    """Construct the rclpy node.  Requires ROS2; raises SystemExit otherwise."""
    try:
        from rclpy.node import Node
        from rclpy.qos import (
            QoSProfile, ReliabilityPolicy, HistoryPolicy,
            qos_profile_sensor_data)
        from sensor_msgs.msg import Image, Imu as ImuMsg
        from std_msgs.msg import Empty
        from rosgraph_msgs.msg import Clock
    except ImportError as e:  # pragma: no cover - environment dependent
        raise SystemExit(
            "rclpy/sensor_msgs not available. Install ROS2 and source it "
            "(e.g. `source /opt/ros/jazzy/setup.bash`), or run the "
            "hardware-free scenario CLI: python -m time_alignment.cli") from e

    if key_file:
        from .storage import load_key
        key = load_key(key_file)
    key = key or secrets.token_bytes(32)

    class AlignerNode(Node):
        def __init__(self) -> None:
            super().__init__("time_aligner")
            base = AlignParams()
            defaults = {k: getattr(base, k) for k in _ROS_PARAM_MAP}
            if overrides:
                defaults.update(overrides)
            for name, default in defaults.items():
                self.declare_parameter(name, default)
            p = AlignParams(**{**defaults, "version": 1})
            store = EvidenceStore(db_path, key,
                                  key_id=os.path.basename(key_file or "ephemeral"))
            self._clock_gen = 0
            self._last_clock_ns: Optional[int] = None
            self.core = AlignerCore(
                p, store,
                clock_ns=lambda: self.get_clock().now().nanoseconds,
                clock_gen_fn=lambda: self._clock_gen)
            qos = QoSProfile(
                depth=64,
                reliability=ReliabilityPolicy.BEST_EFFORT,
                history=HistoryPolicy.KEEP_LAST)
            self.create_subscription(Image, CAMERA_TOPIC,
                                     self._on_camera, qos)
            self.create_subscription(ImuMsg, IMU_TOPIC, self._on_imu, qos)
            self.create_subscription(Empty, RESET_TOPIC, self._on_reset, 10)
            # /clock: a non-monotonic /clock value means sim time restarted.
            self.create_subscription(Clock, "/clock", self._on_clock,
                                     qos_profile_sensor_data)
            self.add_on_set_parameters_callback(self._on_params)
            self.get_logger().info(
                f"aligner ready: db={db_path} tol_ns={p.tolerance_ns} "
                f"reorder_ns={p.reorder_tolerance_ns} exclusive={p.imu_exclusive}")

        def _on_camera(self, msg) -> None:
            rows = self.core.handle_camera(msg)
            for r in rows:
                if r["kind"] == "pair" and r["status"] != "matched":
                    self.get_logger().warn(f"unpaired camera: {r}")
                elif r["kind"] == "pair":
                    self.get_logger().info(
                        f"pair cam={r['camera']['id']} imu={r['imu']['id']} "
                        f"dt={r['dt_ns']/1e6:+.3f}ms seq={r['seq']}")

        def _on_imu(self, msg) -> None:
            self.core.handle_imu(msg)

        def _on_reset(self, msg) -> None:
            # Explicit operator reset (the /clock subscription handles
            # autonomous sim-time restarts).
            self.core.handle_reset("ros_topic_reset")

        def _on_clock(self, msg) -> None:
            t = ros_time_to_ns(msg.clock)
            if self._last_clock_ns is not None and t < self._last_clock_ns:
                # sim time restarted (e.g. bag replay from t=0): new epoch.
                self._clock_gen += 1
                self.core.handle_reset(
                    "clock_backward",
                    {"t_ns": t, "prev_clock_ns": self._last_clock_ns,
                     "clock_generation": self._clock_gen})
            self._last_clock_ns = t

        def _on_params(self, params):
            from rclpy.parameter import Parameter
            from rcl_interfaces.msg import SetParametersResult
            accepted = {p.name: p.value for p in params
                        if p.name in _ROS_PARAM_MAP
                        and p.type_ != Parameter.Type.NOT_SET}
            if not accepted:
                # Always succeed: unrelated parameters must not be rejected.
                return SetParametersResult(successful=True)
            try:
                v = self.core.update_params(**accepted)
                self.get_logger().info(
                    f"params queued: v{v} {accepted} (applies at boundary)")
            except ValueError as e:
                self.get_logger().error(f"rejecting params {accepted}: {e}")
                return SetParametersResult(successful=False, reason=str(e))
            return SetParametersResult(successful=True)

        def destroy_node(self) -> bool:
            self.core.finalize()
            report = self.core.store.verify()
            if not report.ok:
                self.get_logger().error(
                    f"evidence chain invalid at shutdown: {report.first_error}")
            self.core.store.close()
            return super().destroy_node()

    return AlignerNode


def main(argv=None) -> None:  # pragma: no cover - needs live ROS
    import argparse
    try:
        import rclpy
    except ImportError:
        raise SystemExit(
            "ROS2/rclpy not found; use the scenario CLI for hardware-free runs")
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="build/aligner.db")
    ap.add_argument("--key-file", default=None)
    args, ros_args = ap.parse_known_args(argv)
    Path(args.db).parent.mkdir(parents=True, exist_ok=True)
    NodeCls = create_aligner_node(db_path=args.db, key_file=args.key_file)
    rclpy.init(args=ros_args)
    node = NodeCls()
    try:
        rclpy.spin(node)
    except KeyboardInterrupt:
        pass
    finally:
        node.destroy_node()
        rclpy.shutdown()


if __name__ == "__main__":  # pragma: no cover
    main()
