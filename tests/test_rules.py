"""Unit tests for the rules engine — no ROS required."""
import pytest

from qos_diag.qos_model import EndpointQoS, QoSProfile, RULES_VERSION
from qos_diag.rules import Severity, evaluate_match


def ep(rel, dur, hist="KEEP_LAST", depth=10,
       rel_src="rmw_discovery", dur_src="rmw_discovery",
       hist_src="endpoint_announce", depth_src="endpoint_announce"):
    return EndpointQoS(
        profile=QoSProfile(reliability=rel, durability=dur,
                           history=hist, depth=depth),
        provenance={"reliability": rel_src, "durability": dur_src,
                    "history": hist_src, "depth": depth_src})


def match(pub, sub):
    return evaluate_match("/t", "/pub", "/sub", pub, sub)


class TestReliability:
    def test_be_pub_reliable_sub_is_incompatible_R1(self):
        r = match(ep("BEST_EFFORT", "VOLATILE"), ep("RELIABLE", "VOLATILE"))
        assert r.severity == Severity.INCOMPATIBLE
        assert r.blocking_rules == ["R1"]
        assert r.steps[0].decision == "INCOMPATIBLE"

    def test_reliable_pub_be_sub_connects(self):
        r = match(ep("RELIABLE", "VOLATILE"), ep("BEST_EFFORT", "VOLATILE"))
        assert r.severity == Severity.COMPATIBLE
        assert "R1" not in r.blocking_rules

    def test_both_be_compatible(self):
        r = match(ep("BEST_EFFORT", "VOLATILE"), ep("BEST_EFFORT", "VOLATILE"))
        assert r.severity == Severity.COMPATIBLE


class TestDurability:
    def test_volatile_pub_tl_sub_incompatible_R2(self):
        r = match(ep("RELIABLE", "VOLATILE"), ep("RELIABLE", "TRANSIENT_LOCAL"))
        assert r.severity == Severity.INCOMPATIBLE
        assert "R2" in r.blocking_rules

    def test_tl_pub_volatile_sub_connects(self):
        r = match(ep("RELIABLE", "TRANSIENT_LOCAL"), ep("RELIABLE", "VOLATILE"))
        assert r.severity == Severity.COMPATIBLE

    def test_double_mismatch_reports_both_rules(self):
        r = match(ep("BEST_EFFORT", "VOLATILE"),
                  ep("RELIABLE", "TRANSIENT_LOCAL"))
        assert set(r.blocking_rules) == {"R1", "R2"}
        # explainable chain always evaluates all four rule families
        assert [s.rule_id for s in r.steps] == ["R1", "R2", "R3", "R4"]


class TestPerformanceRisk:
    def test_keep_all_is_risk_never_blocking(self):
        r = match(ep("RELIABLE", "VOLATILE", "KEEP_ALL", 10),
                  ep("RELIABLE", "VOLATILE", "KEEP_LAST", 10))
        assert r.severity == Severity.RISK
        assert r.risk_rules == ["R3"]
        assert r.blocking_rules == []

    def test_shallow_subscriber_depth_is_risk(self):
        r = match(ep("RELIABLE", "VOLATILE", "KEEP_LAST", 50),
                  ep("RELIABLE", "VOLATILE", "KEEP_LAST", 2))
        assert r.severity == Severity.RISK
        assert "R4" in r.risk_rules

    def test_equal_depth_compatible(self):
        r = match(ep("RELIABLE", "VOLATILE", "KEEP_LAST", 10),
                  ep("RELIABLE", "VOLATILE", "KEEP_LAST", 10))
        assert r.severity == Severity.COMPATIBLE

    def test_incompatible_outranks_risk(self):
        r = match(ep("BEST_EFFORT", "VOLATILE", "KEEP_ALL", 50),
                  ep("RELIABLE", "VOLATILE", "KEEP_LAST", 1))
        assert r.severity == Severity.INCOMPATIBLE
        assert "R1" in r.blocking_rules
        # risks are still reported in the chain even when blocked
        assert set(r.risk_rules) == {"R3", "R4"}


class TestUnknown:
    def test_unknown_is_not_incompatible(self):
        # never observed yet -> UNKNOWN verdict, never a permanent failure claim
        r = match(ep("UNKNOWN", "UNKNOWN", "UNKNOWN", 0,
                     "unknown", "unknown", "unknown", "unknown"),
                  ep("UNKNOWN", "UNKNOWN", "UNKNOWN", 0,
                     "unknown", "unknown", "unknown", "unknown"))
        assert r.severity == Severity.UNKNOWN
        assert r.blocking_rules == []

    def test_partial_known_still_judges_blocking_rule(self):
        r = match(ep("BEST_EFFORT", "VOLATILE", "UNKNOWN", 0,
                     "rmw_discovery", "rmw_discovery", "unknown", "unknown"),
                  ep("RELIABLE", "VOLATILE", "UNKNOWN", 0,
                     "rmw_discovery", "rmw_discovery", "unknown", "unknown"))
        assert r.severity == Severity.INCOMPATIBLE

    def test_system_default_resolves_to_rmw_defaults(self):
        r = match(ep("SYSTEM_DEFAULT", "SYSTEM_DEFAULT", "SYSTEM_DEFAULT", 0,
                     "rmw_discovery", "rmw_discovery", "rmw_discovery", "unknown"),
                  ep("RELIABLE", "VOLATILE", "KEEP_LAST", 10))
        # defaults: RELIABLE+VOLATILE -> compatible request-wise
        assert "R1" not in r.blocking_rules
        assert "R2" not in r.blocking_rules
        default_steps = [s for s in r.steps
                         if s.publisher_evidence == "inferred_default"]
        assert len(default_steps) >= 2


class TestExplainability:
    def test_report_carries_rules_version_and_explains(self):
        r = match(ep("BEST_EFFORT", "VOLATILE"), ep("RELIABLE", "VOLATILE"))
        assert r.rules_version == RULES_VERSION
        text = r.explain()
        assert "FAIL" in text and "R1" in text
        assert "rmw_discovery" in text
