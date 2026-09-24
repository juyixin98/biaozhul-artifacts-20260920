"""Tests for correlation policies and PSD conservative bounds."""

import numpy as np
import pytest

from app.audit import audit_loops
from app.linalg_utils import (
    is_psd,
    nearest_psd,
    propagate_sum,
    validate_covariance,
    validate_cross_block,
)
from app.models import AuditRequest, CrossBlockSpec, LoopSpec
from tests.factories import edge


def _two_edges(std=1e-3):
    e0 = edge("a", "b", translation=[1, 0, 0], trans_std=std, rot_std=std, edge_id="e0")
    e1 = edge("b", "c", translation=[0, 1, 0], trans_std=std, rot_std=std, edge_id="e1")
    e2 = edge("a", "c", translation=[-1, -1, 0], trans_std=std, rot_std=std, edge_id="e2")
    return [e0, e1, e2]


class TestNearestPSD:
    def test_already_psd_unchanged(self):
        A = np.eye(4) * 1e-6
        Ap, shift = nearest_psd(A)
        assert shift == 0.0
        assert np.allclose(Ap, A)

    def test_negative_eigenvalue_lifted(self):
        A = np.diag([1.0, -2.0, 3.0, 0.5])
        Ap, shift = nearest_psd(A)
        assert is_psd(Ap)
        assert np.isclose(shift, 2.0)
        # positive spectrum preserved
        assert np.allclose(np.diag(Ap), [1, 0, 3, 0.5])

    def test_symmetric_result(self):
        rng = np.random.default_rng(0)
        A = rng.standard_normal((6, 6))
        A = A + A.T
        Ap, _ = nearest_psd(A)
        assert np.allclose(Ap, Ap.T)


class TestCorrelationPolicies:
    def test_independence_policy_echoed_in_findings(self):
        r = audit_loops(
            AuditRequest(
                calibration_version="v",
                correlation_policy="independent",
                edges=_two_edges(),
                loops=[LoopSpec(name="L", frame_ids=["a", "b", "c", "a"])],
            )
        )
        msgs = [f.get("message", "") for f in r["findings"]]
        assert any("EXPLICITLY assumed mutually uncorrelated" in m for m in msgs)

    def test_conservative_rho_psd_and_dominates_independent(self):
        # rho=1 bound covariance must PSD-dominate the rho=0 covariance
        S = np.eye(6) * 1e-6
        J = np.eye(6)
        sig0, _ = propagate_sum([S, S, S], [J, J, J], policy="independent")
        sig1, notes = propagate_sum(
            [S, S, S], [J, J, J], policy="conservative_rho", rho=1.0
        )
        assert is_psd(sig1)
        gap = sig1 - sig0
        assert np.linalg.eigvalsh(0.5 * (gap + gap.T)).min() >= -1e-12
        assert any(n["kind"] == "conservative_over_bound" for n in notes)

    def test_conservative_rho_scales_with_rho(self):
        S = np.eye(6) * 1e-6
        J = np.eye(6)
        s0, _ = propagate_sum([S, S], [J, J], policy="conservative_rho", rho=0.0)
        s5, _ = propagate_sum([S, S], [J, J], policy="conservative_rho", rho=0.5)
        s1, _ = propagate_sum([S, S], [J, J], policy="conservative_rho", rho=1.0)
        # trace grows monotonically with rho
        assert np.trace(s5) > np.trace(s0)
        assert np.trace(s1) > np.trace(s5)

    def test_cross_block_included_symmetrized(self):
        s2 = 1e-6
        S = np.eye(6) * s2
        C = 0.5 * s2 * np.eye(6)
        sig, _ = propagate_sum(
            [S, S], [np.eye(6), np.eye(6)],
            policy="cross_blocks", cross_blocks={(0, 1): C},
        )
        # diagonal = S + S + C + C^T = 3 s2
        assert np.allclose(np.diag(sig), 3 * s2)

    def test_cross_block_full_contribution_not_halved(self):
        # regression: the off-diagonal contribution must count twice
        s2 = 2.0e-6
        S = np.eye(6) * s2
        C = 0.25 * s2 * np.eye(6)
        sig, _ = propagate_sum(
            [S, S], [np.eye(6), np.eye(6)],
            policy="cross_blocks", cross_blocks={(0, 1): C},
        )
        expected_diag = 2 * s2 + 2 * 0.25 * s2
        assert np.allclose(np.diag(sig), expected_diag)

    def test_cross_block_validation_psd_joint(self):
        S = np.eye(6) * 1e-6
        # C = 2S makes the joint block indefinite
        w_bad = validate_cross_block(2.0 * S, S, S)
        assert w_bad is not None and w_bad < 0
        w_ok = validate_cross_block(0.3 * S, S, S)
        assert w_ok is not None and w_ok >= -1e-12

    def test_conservative_rho_requires_rho(self):
        r = audit_loops(
            AuditRequest(
                calibration_version="v",
                correlation_policy="conservative_rho",
                rho=None,
                edges=_two_edges(),
                loops=[LoopSpec(name="L", frame_ids=["a", "b", "c", "a"])],
            )
        )
        assert any(f["kind"] == "rho_missing" for f in r["findings"])

    def test_rho_out_of_range_flagged(self):
        r = audit_loops(
            AuditRequest(
                calibration_version="v",
                correlation_policy="conservative_rho",
                rho=1.5,
                edges=_two_edges(),
                loops=[LoopSpec(name="L", frame_ids=["a", "b", "c", "a"])],
            )
        )
        assert any(f["kind"] == "rho_out_of_range" for f in r["findings"])

    def test_rho_bad_shape_flagged(self):
        r = audit_loops(
            AuditRequest(
                calibration_version="v",
                correlation_policy="conservative_rho",
                rho=[[0.1, 0.2], [0.3, 0.4]],
                edges=_two_edges(),
                loops=[LoopSpec(name="L", frame_ids=["a", "b", "c", "a"])],
            )
        )
        assert any(f["kind"] == "rho_bad_shape" for f in r["findings"])

    def test_cross_block_unknown_edge_flagged(self):
        r = audit_loops(
            AuditRequest(
                calibration_version="v",
                correlation_policy="cross_blocks",
                edges=_two_edges(),
                cross_blocks=[
                    CrossBlockSpec(i="e0", j="ghost", block=np.eye(6).tolist())
                ],
                loops=[LoopSpec(name="L", frame_ids=["a", "b", "c", "a"])],
            )
        )
        assert any(f["kind"] == "unknown_cross_block_edge" for f in r["findings"])


class TestNoSilentIndependence:
    def test_policy_is_required_field(self):
        import pydantic

        with pytest.raises(pydantic.ValidationError):
            AuditRequest(calibration_version="v", edges=_two_edges())
