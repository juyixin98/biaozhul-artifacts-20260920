"""Wire protocol: signed command envelopes and signed mapping-epoch messages.

Payloads are JSON carried in ``std_msgs/msg/String``.  Every command envelope
is signed end-to-end by the gateway using the *tester's* key, so the robot can
authenticate the real cryptographic identity of the command independent of
DDS.  Epoch messages are signed with the registry ``epoch_key`` and pin the
robot to one mapping version; a command whose epoch is not the robot's current
epoch is cryptographically tied to an old authorisation and is dropped.
"""

from __future__ import annotations

import json
import time
from typing import Any

from .crypto import canonical_json, hmac_sign, hmac_verify

ENVELOPE_VERSION = 1


def build_command_envelope(
    *,
    robot_id: str,
    namespace: str,
    target: str,
    seq: int,
    command: dict[str, Any],
    expires_at: float,
    issued_at: float,
    epoch: int,
    tester: str,
    tester_key: str,
    command_id: str,
) -> dict[str, Any]:
    envelope = {
        "v": ENVELOPE_VERSION,
        "id": command_id,
        "robot_id": robot_id,
        "namespace": namespace,
        "target": target,
        "seq": int(seq),
        "command": command,
        "expires_at": float(expires_at),
        "issued_at": float(issued_at),
        "epoch": int(epoch),
        "tester": tester,
    }
    envelope["sig"] = hmac_sign(canonical_json(envelope), tester_key)
    return envelope


def build_epoch_message(
    *, robot_id: str, namespace: str, epoch: int, ts: float, epoch_key: str
) -> dict[str, Any]:
    msg = {
        "robot_id": robot_id,
        "namespace": namespace,
        "epoch": int(epoch),
        "ts": float(ts),
    }
    msg["sig"] = hmac_sign(canonical_json(msg), epoch_key)
    return msg


def verify_command_envelope(
    envelope: Any,
    *,
    expected_robot_id: str,
    expected_namespace: str,
    current_epoch: int,
    tester_keys: dict[str, str],
    now: float,
    seen_ids: set[str] | None = None,
) -> tuple[bool, str | None]:
    """Pure verification used by synthetic robots (no rclpy calls).

    Returns ``(ok, reason)``.  ``reason`` records why a command was dropped.
    """
    if not isinstance(envelope, dict):
        return False, "malformed_json"
    required = {
        "v",
        "id",
        "robot_id",
        "namespace",
        "target",
        "seq",
        "command",
        "expires_at",
        "issued_at",
        "epoch",
        "tester",
        "sig",
    }
    if set(envelope.keys()) != required:
        return False, "malformed_envelope"
    if envelope["v"] != ENVELOPE_VERSION:
        return False, "bad_version"

    sig = envelope.get("sig")
    tester = envelope.get("tester")
    key = tester_keys.get(tester) if isinstance(tester, str) else None
    if key is None:
        return False, "unknown_tester"

    signed_part = {k: v for k, v in envelope.items() if k != "sig"}
    if not hmac_verify(canonical_json(signed_part), sig, key):
        return False, "bad_signature"

    # Structural binding: a signed envelope for another robot/namespace must
    # not be accepted here even if it somehow reaches this topic.
    if envelope["robot_id"] != expected_robot_id:
        return False, "robot_mismatch"
    if envelope["namespace"] != expected_namespace:
        return False, "namespace_mismatch"
    if not isinstance(envelope["epoch"], int) or envelope["epoch"] != current_epoch:
        return False, "stale_epoch"
    if not isinstance(envelope["target"], str) or not envelope["target"]:
        return False, "bad_target"
    if not isinstance(envelope["seq"], int) or envelope["seq"] < 0:
        return False, "bad_seq"
    if not isinstance(envelope["command"], dict):
        return False, "bad_command"
    if float(envelope["expires_at"]) <= now:
        return False, "expired_on_arrival"
    if seen_ids is not None and envelope["id"] in seen_ids:
        return False, "duplicate_delivery"
    return True, None


def encode_json(obj: Any) -> str:
    return json.dumps(obj, separators=(",", ":"), ensure_ascii=False)


def decode_json(data: str) -> Any:
    return json.loads(data)


def clock() -> float:
    return time.time()
