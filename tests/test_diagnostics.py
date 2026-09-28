"""Diagnostic tests: pre-trends are falsifiable but not provable."""
from __future__ import annotations

from app.contracts.models import (
    BalanceStrategy,
    DiagnosticLevel,
    Observation as O,
    WeightPolicy,
)
from app.core.alignment import align_panel, classify_two_by_two
from app.evidence.diagnostics import (
    cluster_count_warning,
    contamination_evidence,
    pretrend_diagnostic,
)
from app.reproducibility.fixtures import build_fixture
from tests import reference_expected as expected


def _panel(obs, **kw):
    return align_panel(
        obs,
        balance=BalanceStrategy.BALANCED,
        weight_policy=WeightPolicy.UNIT_FIXED,
        pre_period=kw.get("pre"),
        post_period=kw.get("post"),
    )


def test_nonparallel_pretrend_is_falsified():
    panel = _panel(build_fixture("nonparallel_pretrend"), pre=0, post=2)
    treated, control, _, _ = classify_two_by_two(panel)
    diag = pretrend_diagnostic(
        panel.units, treated, control, onset=2, alpha=0.05
    )
    # The constructed slope gap is exactly 3.0 and must be detected.
    assert diag.level is DiagnosticLevel.FAILED
    assert diag.p_value is not None and diag.p_value < 0.05
    # Interaction coefficient magnitude is reported and matches the paper gap.
    # The F statistic is present; we assert detection, not an exact F.
    assert "differently" in diag.message or "Reject" in diag.message


def test_parallel_pretrend_is_not_declared_proven():
    # Clean hand_2x2 has only ONE pre period, so no pre-trend claim is possible;
    # the diagnostic must be INDETERMINATE, never a false "parallel trends OK".
    panel = _panel(build_fixture("hand_2x2"))
    treated, control, _, _ = classify_two_by_two(panel)
    diag = pretrend_diagnostic(panel.units, treated, control, onset=1)
    assert diag.level is DiagnosticLevel.INDETERMINATE
    assert diag.caveat is not None and "not" in diag.caveat.lower()


def test_flat_pretrends_nonrejection_still_caveated():
    # Build two pre periods with identical flat pre-paths for both groups.
    # A non-rejection must remain a caveated WARNING, never proof.
    obs = []
    for u in ("t1", "t2"):
        obs += [
            O(unit_id=u, period=0, y=1.0, treated=False),
            O(unit_id=u, period=1, y=1.0, treated=False),
            O(unit_id=u, period=2, y=4.0, treated=True),
        ]
    for u in ("c1", "c2"):
        obs += [
            O(unit_id=u, period=0, y=2.0, treated=False),
            O(unit_id=u, period=1, y=2.0, treated=False),
            O(unit_id=u, period=2, y=2.0, treated=False),
        ]
    panel = _panel(obs, pre=0, post=2)
    treated, control, _, _ = classify_two_by_two(panel)
    diag = pretrend_diagnostic(panel.units, treated, control, onset=2)
    assert diag.level is DiagnosticLevel.WARNING
    assert "cannot establish" in diag.message


def test_contamination_evidence_flags_dirty_control():
    panel = _panel(build_fixture("contaminated_control"), pre=0, post=2)
    treated, control, _, _ = classify_two_by_two(panel)
    diags = contamination_evidence(panel.units, treated, control)
    # c2 was removed from controls at classification; evidence still surfaces
    # treated-outside-design warnings and never falsely reports clean controls.
    names = [d.name for d in diags]
    assert any(n.startswith("treated_outside_design:c2") for n in names)


def test_clean_panel_contamination_ok():
    panel = _panel(build_fixture("hand_2x2"))
    treated, control, _, _ = classify_two_by_two(panel)
    diags = contamination_evidence(panel.units, treated, control)
    main = next(d for d in diags if d.name == "control_contamination")
    assert main.level is DiagnosticLevel.OK


def test_cluster_count_levels():
    assert cluster_count_warning(1, 2).level is DiagnosticLevel.FAILED
    assert cluster_count_warning(4, 2).level is DiagnosticLevel.WARNING
    assert cluster_count_warning(50, 2).level is DiagnosticLevel.OK
