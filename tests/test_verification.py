"""Unit tests for verification report classification (fail vs uncertain)."""

from __future__ import annotations

import numpy as np

from adam_shard.verification import (
    STATUS_FAIL,
    STATUS_PASS,
    STATUS_UNCERTAIN,
    VerificationReport,
    compare_param_dicts,
    max_abs_rel_diff,
    _classify_tolerance,
)


def test_classify_pass_fail_uncertain_bands():
    assert _classify_tolerance(0.0, 0.0, rtol=1e-8, atol=1e-10)[0] == STATUS_PASS
    # Within tolerance but close to the band -> uncertain.
    status, reason = _classify_tolerance(5e-11, 0.0, rtol=1e-8, atol=1e-10)
    assert status == STATUS_UNCERTAIN and reason
    # Outside tolerance -> hard failure with a numeric reason.
    status, reason = _classify_tolerance(2e-8, 0.0, rtol=1e-8, atol=1e-10)
    assert status == STATUS_FAIL and "exceeded" in reason


def test_report_aggregates_status_and_keeps_failures_separate():
    from adam_shard.verification import CheckEntry

    report = VerificationReport(
        request_id="req-x",
        stage="unit",
        version="1.0.0",
        location="memory",
        checks=(
            CheckEntry("ok-a", STATUS_PASS, {}),
            CheckEntry("warn-b", STATUS_UNCERTAIN, {"reason": "near band"}),
            CheckEntry("bad-c", STATUS_FAIL, {"reason": "diverged"}),
        ),
    )
    assert report.status == STATUS_FAIL
    assert [c.name for c in report.failures] == ["bad-c"]
    assert [c.name for c in report.uncertainties] == ["warn-b"]
    payload = report.to_dict()
    assert payload["failures"][0]["reason"] == "diverged"
    assert payload["uncertainties"][0]["reason"] == "near band"


def test_report_all_passing_is_pass():
    from adam_shard.verification import CheckEntry

    report = VerificationReport(
        request_id="req-y", stage="unit", version="1.0.0", location="memory",
        checks=(CheckEntry("a", STATUS_PASS, {}), CheckEntry("b", STATUS_PASS, {})),
    )
    assert report.status == STATUS_PASS and report.failures == ()


def test_compare_param_dicts_detects_name_and_shape_differences():
    a = {"w": np.zeros((2, 2)), "b": np.ones(2)}
    missing = {"w": np.zeros((2, 2))}
    entry = compare_param_dicts(a, missing, label="cmp")
    assert entry.status == STATUS_FAIL
    assert "differ" in entry.detail["reason"]

    wrong_shape = {"w": np.zeros((2, 3)), "b": np.ones(2)}
    entry = compare_param_dicts(a, wrong_shape, label="cmp2")
    assert entry.status == STATUS_FAIL and entry.detail["parameter"] == "w"

    equal = {"w": np.zeros((2, 2)), "b": np.ones(2)}
    assert compare_param_dicts(a, equal, label="cmp3").status == STATUS_PASS


def test_max_abs_rel_diff_values():
    a, r = max_abs_rel_diff(np.array([1.0, 2.0]), np.array([1.0, 2.0]))
    assert a == 0.0 and r == 0.0
    a, r = max_abs_rel_diff(np.array([1.0, 2.0]), np.array([1.5, 3.0]))
    assert abs(a - 1.0) < 1e-12 and r > 0.3
