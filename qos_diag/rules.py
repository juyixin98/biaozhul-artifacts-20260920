"""QoS compatibility rules engine.

Given a publisher endpoint and a subscriber endpoint on the *same resolved
topic*, produce a MatchReport that distinguishes:

  * INCOMPATIBLE  — DDS will refuse to connect (definite protocol-level mismatch)
  * RISK          — endpoints connect, but message loss / latency under load is
                    plausible (performance-only)
  * COMPATIBLE    — connects and no performance rule fires

Every verdict carries an ordered, human-readable "match chain":
rule-id -> evidence -> decision, so results are explainable and re-testable.

References (rmw / DDS QoS compatibility):
  - reliability: subscriber RELIABLE + publisher BEST_EFFORT => INCOMPATIBLE
                 (request-offer model; RELIABLE req cannot be satisfied by BE offer)
  - durability : subscriber TRANSIENT_LOCAL + publisher VOLATILE => INCOMPATIBLE
                 (late-joining subscriber requires latched offer)
  - history/depth never block a match in DDS; they bound resource use => RISK only
"""
from __future__ import annotations

from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field

from .qos_model import (
    DEFAULT_PROFILE,
    EndpointQoS,
    EvidenceSource,
    QoSProfile,
    RULES_VERSION,
)


class Severity(str, Enum):
    INCOMPATIBLE = "INCOMPATIBLE"
    RISK = "RISK"
    COMPATIBLE = "COMPATIBLE"
    UNKNOWN = "UNKNOWN"  # insufficient evidence; not judged compatible/incompatible


class ChainStep(BaseModel):
    rule_id: str
    attribute: str
    publisher_value: Optional[str | int] = None
    subscriber_value: Optional[str | int] = None
    publisher_evidence: Optional[str] = None
    subscriber_evidence: Optional[str] = None
    decision: str          # PASS | INCOMPATIBLE | RISK | SKIP_UNKNOWN
    rationale: str


class MatchReport(BaseModel):
    topic: str
    publisher_node: str
    subscriber_node: str
    publisher_gid: Optional[str] = None
    subscriber_gid: Optional[str] = None
    severity: Severity
    steps: list[ChainStep] = Field(default_factory=list)
    blocking_rules: list[str] = Field(default_factory=list)
    risk_rules: list[str] = Field(default_factory=list)
    rules_version: str = RULES_VERSION
    message_type: Optional[str] = None

    def explain(self) -> str:
        lines = [f"[{self.severity.value}] {self.topic}: "
                 f"pub({self.publisher_node}) -> sub({self.subscriber_node})"]
        for s in self.steps:
            tag = {"PASS": "ok  ", "INCOMPATIBLE": "FAIL",
                   "RISK": "risk", "SKIP_UNKNOWN": "??  "}[s.decision]
            lines.append(
                f"  {tag} {s.rule_id} {s.attribute}: "
                f"pub={s.publisher_value}({s.publisher_evidence or '?'}) vs "
                f"sub={s.subscriber_value}({s.subscriber_evidence or '?'}) — {s.rationale}"
            )
        return "\n".join(lines)


def _resolve(attr: str, ep: EndpointQoS) -> tuple[object, str]:
    """Return (value, evidence) resolving SYSTEM_DEFAULT to rmw default."""
    val = getattr(ep.profile, attr)
    src = ep.source_of(attr)
    if attr in ("reliability", "durability", "history") and val == "SYSTEM_DEFAULT":
        return DEFAULT_PROFILE[attr], EvidenceSource.INFERRED_DEFAULT
    return val, src


def _step(rule_id: str, attr: str, pub: EndpointQoS, sub: EndpointQoS,
          decision: str, rationale: str, pv=None, sv=None, pe=None, se=None) -> ChainStep:
    if pv is None:
        pv, pe = _resolve(attr, pub)
    if sv is None:
        sv, se = _resolve(attr, sub)
    return ChainStep(
        rule_id=rule_id, attribute=attr,
        publisher_value=pv, subscriber_value=sv,
        publisher_evidence=pe, subscriber_evidence=se,
        decision=decision, rationale=rationale,
    )


def evaluate_match(topic: str, pub_node: str, sub_node: str,
                   pub: EndpointQoS, sub: EndpointQoS,
                   pub_gid: str | None = None, sub_gid: str | None = None,
                   message_type: str | None = None) -> MatchReport:
    """Run all rules in a fixed order; first INCOMPATIBLE rule decides severity,
    but every rule is evaluated so the chain is complete."""
    steps: list[ChainStep] = []
    blocking: list[str] = []
    risks: list[str] = []

    # R1 reliability (request/offer) ----------------------------------------
    pr, pre = _resolve("reliability", pub)
    sr, sre = _resolve("reliability", sub)
    if pr == "UNKNOWN" or sr == "UNKNOWN":
        steps.append(_step("R1", "reliability", pub, sub, "SKIP_UNKNOWN",
                           "reliability not observed on either side; cannot judge",
                           pr, sr, pre, sre))
        unknown = True
    elif sr == "RELIABLE" and pr == "BEST_EFFORT":
        steps.append(_step("R1", "reliability", pub, sub, "INCOMPATIBLE",
                           "subscriber REQUESTS reliable delivery but publisher only "
                           "OFFERS best-effort; DDS will not match these endpoints",
                           pr, sr, pre, sre))
        blocking.append("R1")
        unknown = False
    else:
        steps.append(_step("R1", "reliability", pub, sub, "PASS",
                           "publisher's offer satisfies subscriber's reliability request",
                           pr, sr, pre, sre))
        unknown = False

    # R2 durability (request/offer) -----------------------------------------
    pd, pde = _resolve("durability", pub)
    sd, sde = _resolve("durability", sub)
    if pd == "UNKNOWN" or sd == "UNKNOWN":
        steps.append(_step("R2", "durability", pub, sub, "SKIP_UNKNOWN",
                           "durability not observed; cannot judge",
                           pd, sd, pde, sde))
        unknown_dur = True
    elif sd == "TRANSIENT_LOCAL" and pd == "VOLATILE":
        steps.append(_step("R2", "durability", pub, sub, "INCOMPATIBLE",
                           "subscriber REQUESTS TRANSIENT_LOCAL (latched history) but "
                           "publisher only OFFERS VOLATILE; late-joining/latched "
                           "requirement cannot be met — DDS refuses the match",
                           pd, sd, pde, sde))
        blocking.append("R2")
        unknown_dur = False
    else:
        steps.append(_step("R2", "durability", pub, sub, "PASS",
                           "publisher's durability offer satisfies subscriber request",
                           pd, sd, pde, sde))
        unknown_dur = False

    # R3 history policy — never blocks; KEEP_ALL is a resource risk --------
    ph, phe = _resolve("history", pub)
    sh, she = _resolve("history", sub)
    if ph == "UNKNOWN" and sh == "UNKNOWN":
        steps.append(_step("R3", "history", pub, sub, "SKIP_UNKNOWN",
                           "history policy is not carried in DDS discovery and no "
                           "endpoint announcement received",
                           ph, sh, phe, she))
        unknown_hist = True
    else:
        unknown_hist = False
        keep_all_sides = [name for name, v in (("publisher", ph), ("subscriber", sh))
                          if v == "KEEP_ALL"]
        if keep_all_sides:
            steps.append(_step("R3", "history", pub, sub, "RISK",
                               "KEEP_ALL on " + " and ".join(keep_all_sides) +
                               " means unbounded buffering on slow consumers; "
                               "connectivity is unaffected but memory may grow",
                               ph, sh, phe, she))
            risks.append("R3")
        else:
            steps.append(_step("R3", "history", pub, sub, "PASS",
                               "bounded KEEP_LAST on both sides (or defaults)",
                               ph, sh, phe, she))

    # R4 depth (KEEP_LAST queue size) — perf-only; DDS ignores for matching
    pdp, pdpe = _resolve("depth", pub)
    sdp, sdpe = _resolve("depth", sub)
    if not isinstance(pdp, int) or not isinstance(sdp, int) or pdp <= 0 or sdp <= 0:
        steps.append(_step("R4", "depth", pub, sub, "SKIP_UNKNOWN",
                           "depth is not carried in DDS discovery (Fast-DDS reports 0); "
                           "needs endpoint announcement",
                           pdp, sdp, pdpe, sdpe))
        unknown_depth = True
    else:
        unknown_depth = False
        # Small-subscriber-depth relative to publisher burst capacity is a
        # loss-under-load risk. Threshold: sub depth < min(10, pub depth).
        if sdp < pdp and sdp < 10:
            steps.append(_step("R4", "depth", pub, sub, "RISK",
                               f"subscriber KEEP_LAST depth {sdp} is smaller than "
                               f"publisher depth {pdp}; bursts faster than the "
                               f"subscriber drains may overwrite/drop messages. "
                               f"Connectivity unaffected.",
                               pdp, sdp, pdpe, sdpe))
            risks.append("R4")
        elif pdp < sdp:
            steps.append(_step("R4", "depth", pub, sub, "PASS",
                               f"publisher depth {pdp} <= subscriber depth {sdp}; "
                               f"subscriber buffer can hold publisher bursts",
                               pdp, sdp, pdpe, sdpe))
        else:
            steps.append(_step("R4", "depth", pub, sub, "PASS",
                               f"comparable depths (pub={pdp}, sub={sdp})",
                               pdp, sdp, pdpe, sdpe))

    if blocking:
        severity = Severity.INCOMPATIBLE
    elif unknown or unknown_dur:
        # A blocking policy (reliability/durability) is not observed on at
        # least one side: we cannot confirm DDS would match, so stay UNKNOWN
        # rather than claim COMPATIBLE. Never a failure verdict either.
        severity = Severity.UNKNOWN
    else:
        severity = Severity.RISK if risks else Severity.COMPATIBLE

    return MatchReport(
        topic=topic, publisher_node=pub_node, subscriber_node=sub_node,
        publisher_gid=pub_gid, subscriber_gid=sub_gid,
        severity=severity, steps=steps, blocking_rules=blocking,
        risk_rules=risks, message_type=message_type,
    )
