"""Endpoints announcement protocol.

DDS discovery (Fast-DDS via rmw) conveys reliability and durability but NOT
history/depth. Each instrumented node therefore publishes a small JSON beacon
on the well-known latched topic ``/qos_diag/endpoints`` describing every
publisher/subscription it created, including its configured depth.

The payload is JSON inside std_msgs/String so no custom rosidl package is
required. The monitor fuses beacons with real DDS-discovered endpoints.
"""
from __future__ import annotations

import json
import time
from typing import Any

ANNOUNCE_TOPIC = "/qos_diag/endpoints"
PROTOCOL = "qosdiag-endpoint-announce/1"

# latched + reliable + keep-all-ish depth so late-joining monitors receive
# beacons from already-running nodes.
ANNOUNCE_QOS = {
    "reliability": "RELIABLE",
    "durability": "TRANSIENT_LOCAL",
    "history": "KEEP_LAST",
    "depth": 100,
}


def build_beacon(*, node_name: str, node_namespace: str, role: str, topic: str,
                 message_type: str, reliability: str, durability: str,
                 history: str, depth: int, seq: int) -> str:
    payload: dict[str, Any] = {
        "protocol": PROTOCOL,
        "seq": seq,
        "sent_ts": time.time(),
        "node_name": node_name,
        "node_namespace": node_namespace,
        "role": role,                  # "publisher" | "subscription"
        "topic": topic,
        "message_type": message_type,
        "gid": None,                  # rclpy does not expose local GID in Jazzy;
                                      # monitor correlates on (topic,node,role,ns)
        "qos": {
            "reliability": reliability,
            "durability": durability,
            "history": history,
            "depth": depth,
        },
    }
    return json.dumps(payload, ensure_ascii=False)


def parse_beacon(raw: str) -> dict[str, Any] | None:
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return None
    if data.get("protocol") != PROTOCOL:
        return None
    q = data.get("qos") or {}
    if not all(k in q for k in ("reliability", "durability", "history", "depth")):
        return None
    if data.get("role") not in ("publisher", "subscription"):
        return None
    if not data.get("topic") or not data.get("node_name"):
        return None
    return data
