"""QoS value objects and enums.

Policy integers mirror the rmw/dds wire values (rclpy enums use the same
numeric encoding) so data captured from a real graph can be compared directly:

  reliability: 1 = RELIABLE, 2 = BEST_EFFORT, 0 = SYSTEM_DEFAULT/UNKNOWN-handled
  durability : 1 = TRANSIENT_LOCAL, 2 = VOLATILE, 3 = TRANSIENT_LOCAL? -> see rclpy
  history    : 1 = KEEP_LAST, 2 = KEEP_ALL, 0 = SYSTEM_DEFAULT, 3 = UNKNOWN

We always canonicalise to *names* at the boundary.
"""
from __future__ import annotations

from enum import IntEnum
from typing import Optional

from pydantic import BaseModel, Field


# Rules engine version. Bump RULES_VERSION on every semantic change to the
# matching logic; every persisted snapshot records the version it was judged by.
RULES_VERSION = "1.0.0"
RULES_SCHEMA_HASH_ALGO = "sha256"


class Reliability(IntEnum):
    SYSTEM_DEFAULT = 0
    RELIABLE = 1
    BEST_EFFORT = 2
    UNKNOWN = 3


class Durability(IntEnum):
    SYSTEM_DEFAULT = 0
    TRANSIENT_LOCAL = 1
    VOLATILE = 2
    UNKNOWN = 3


class History(IntEnum):
    SYSTEM_DEFAULT = 0
    KEEP_LAST = 1
    KEEP_ALL = 2
    UNKNOWN = 3


# Where a QoS attribute value came from. This matters: rmw discovery on
# Fast-DDS only conveys reliability/durability; history/depth are NOT on the
# wire and must be supplemented by the endpoint's out-of-band announcement.
class EvidenceSource:
    RMW_DISCOVERY = "rmw_discovery"   # observed via get_*_info_by_topic (DDS SDP)
    ENDPOINT_ANNOUNCE = "endpoint_announce"  # endpoint's own /qos_diag/endpoints beacon
    INFERRED_DEFAULT = "inferred_default"    # only after negotiation resolution
    UNKNOWN = "unknown"


def reliability_name(v) -> str:
    try:
        return Reliability(int(v)).name
    except (ValueError, TypeError):
        return "UNKNOWN"


def durability_name(v) -> str:
    try:
        return Durability(int(v)).name
    except (ValueError, TypeError):
        return "UNKNOWN"


def history_name(v) -> str:
    try:
        return History(int(v)).name
    except (ValueError, TypeError):
        return "UNKNOWN"


# rclpy/qos default resolution (rmw_qos_profile_default):
#   reliability -> RELIABLE, durability -> VOLATILE, history -> KEEP_LAST, depth -> 10
DEFAULT_PROFILE = {
    "reliability": "RELIABLE",
    "durability": "VOLATILE",
    "history": "KEEP_LAST",
    "depth": 10,
}


class QoSProfile(BaseModel):
    """One endpoint's four policies we diagnose. depth<=0 with KEEP_LAST means unknown."""

    reliability: str
    durability: str
    history: str
    depth: int = Field(ge=0)

    def normalized(self) -> "QoSProfile":
        """Resolve SYSTEM_DEFAULT / UNKNOWN policies to the rmw default.

        KEEP_LAST with depth 0 from the wire is treated as depth-unknown and
        marked separately by the caller (via provenance); here we keep 0.
        """
        return QoSProfile(
            reliability=DEFAULT_PROFILE["reliability"]
            if self.reliability in ("SYSTEM_DEFAULT",) else self.reliability,
            durability=DEFAULT_PROFILE["durability"]
            if self.durability in ("SYSTEM_DEFAULT",) else self.durability,
            history=DEFAULT_PROFILE["history"]
            if self.history in ("SYSTEM_DEFAULT",) else self.history,
            depth=self.depth,
        )


class EndpointQoS(BaseModel):
    """Observed profile for one endpoint plus per-attribute provenance."""

    profile: QoSProfile
    # attribute -> EvidenceSource
    provenance: dict[str, str] = Field(default_factory=dict)

    def source_of(self, attr: str) -> str:
        return self.provenance.get(attr, EvidenceSource.UNKNOWN)
