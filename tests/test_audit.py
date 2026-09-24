"""Tests for graph parsing, validation and evidence reporting."""

import numpy as np
import pytest

from app.audit import audit_chain, audit_loops
from app.models import AuditRequest, ChainRequest, LoopSpec
from tests.factories import edge, iso_cov, rotated_edge_z


def _req(edges, loops=None, policy="independent", **kw):
    return AuditRequest(
        calibration_version="test-v1",
        correlation_policy=policy,
        edges=edges,
        loops=loops,
        **kw,
    )


class TestRotationValidation:
    def test_non_orthogonal_matrix_rejected(self):
        bad = edge("a", "b", rotation=np.eye(3) + 0.01 * np.array(
            [[0, 1, 0], [0, 0, 0], [0, 0, 0]]), edge_id="badrot")
        r = audit_loops(_req([bad]))
        assert r["fatal"] is True
        f = next(x for x in r["findings"] if x["kind"] == "illegal_rotation")
        assert f["edge_id"] == "badrot"
        assert f["detail"] == "not_orthogonal"
        assert f["max_abs_RTR_minus_I"] > 1e-6

    def test_reflection_det_minus_one_rejected(self):
        R = np.diag([1.0, 1.0, -1.0])  # orthogonal but det=-1
        r = audit_loops(_req([edge("a", "b", rotation=R)]))
        f = next(x for x in r["findings"] if x["kind"] == "illegal_rotation")
        assert f["detail"] == "det_not_plus_one"

    def test_non_unit_quaternion_rejected(self):
        r = audit_loops(_req([edge("a", "b", quat=[1.0, 0.2, 0.0, 0.0])]))
        f = next(x for x in r["findings"] if x["kind"] == "illegal_rotation")
        assert f["detail"] == "quaternion_not_unit"
        assert f["norm"] > 1.01

    def test_unit_quaternion_accepted(self):
        q = [np.cos(0.2), 0, 0, np.sin(0.2)]
        r = audit_loops(_req([edge("a", "b", quat=q)]))
        assert not any(x["kind"] == "illegal_rotation" for x in r["findings"])


class TestCovarianceValidation:
    def test_negative_eigenvalue_rejected_with_evidence(self):
        bad_cov = np.diag([-1e-6, 1e-6, 1e-6, 1e-7, 1e-7, 1e-7])
        e = edge("a", "b", cov=bad_cov, edge_id="badcov")
        r = audit_loops(_req([e]))
        assert r["fatal"] is True
        f = next(x for x in r["findings"] if x["kind"] == "covariance_not_psd")
        assert f["name"] == "badcov"
        assert f["min_eigenvalue"] < 0
        assert r["summary"]["status"] == "rejected"

    def test_non_square_covariance_rejected(self):
        e = edge("a", "b", cov=np.ones((6, 5)), edge_id="weird")
        r = audit_loops(_req([e]))
        assert any(x["kind"] == "covariance_not_square" for x in r["findings"])

    def test_nan_covariance_rejected(self):
        cov = np.full((6, 6), np.nan)
        r = audit_loops(_req([edge("a", "b", cov=cov)]))
        assert any(x["kind"] == "covariance_not_finite" for x in r["findings"])

    def test_tiny_numerical_negativity_tolerated(self):
        # -1e-14 eigenvalue is float noise around zero; must be accepted
        S = np.eye(6) * 1e-8
        S[0, 0] = -1e-14
        r = audit_loops(_req([edge("a", "b", cov=S)]))
        assert not r["fatal"]


class TestConvention:
    def test_mixed_convention_rejected(self):
        e1 = edge("a", "b", edge_id="e1")
        e2 = edge("b", "c", edge_id="e2", convention="left")
        r = audit_loops(_req([e1, e2], convention="right"))
        f = next(x for x in r["findings"] if x["kind"] == "mixed_convention")
        assert f["edge_id"] == "e2"
        assert r["fatal"] is True

    def test_duplicate_edge_id_rejected(self):
        e1 = edge("a", "b", edge_id="dup")
        e2 = edge("b", "c", edge_id="dup")
        r = audit_loops(_req([e1, e2]))
        assert any(x["kind"] == "duplicate_edge_id" for x in r["findings"])
        assert r["fatal"] is True


class TestLoopClosure:
    def _triangle(self, angle=0.1, perturb_t=(0.0, 0.0, 0.0), cov_scale=1.0):
        # Edges map child -> parent: e0 maps b->a (T1), e1 maps c->b (T2),
        # e2 maps c->a directly (T3). Walk a->b->c traverses e0,e1 in reverse
        # (T1^-1, T2^-1); c->a traverses e2 forward (T3). Walk product is
        # T3 T2^-1 T1^-1; exact closure needs T3 = (T2^-1 T1^-1)^-1 = T1 T2.
        t1 = np.array([1.0, 0.0, 0.0])
        R1 = np.eye(3)
        from app.se3 import make_T
        T1 = make_T(R1, t1)
        c, s = np.cos(angle), np.sin(angle)
        R2 = np.array([[c, -s, 0], [s, c, 0], [0, 0, 1]])
        t2 = np.array([0.0, 1.0, 0.0])
        T2 = make_T(R2, t2)
        T3 = T1 @ T2  # direct c->a edge for an exactly closing triangle
        st = 1e-3 * cov_scale
        sr = 1e-3 * cov_scale
        e0 = edge("a", "b", translation=t1, rotation=R1, trans_std=st, rot_std=sr, edge_id="e0")
        e1 = edge("b", "c", translation=t2, rotation=R2, trans_std=st, rot_std=sr, edge_id="e1")
        e2 = edge(
            "a",
            "c",
            translation=T3[:3, 3] + np.array(perturb_t),
            rotation=T3[:3, :3],
            trans_std=st,
            rot_std=sr,
            edge_id="e2",
        )
        return [e0, e1, e2]

    def test_exact_closure_zero_residual(self):
        edges = self._triangle()
        r = audit_loops(
            _req(edges, loops=[LoopSpec(name="L", frame_ids=["a", "b", "c", "a"])])
        )
        lp = r["loops"][0]
        assert np.linalg.norm(lp["residual"]["translation"]) < 1e-9
        assert np.linalg.norm(lp["residual"]["rotation_axisangle_rad"]) < 1e-9
        assert lp["closure_test"]["status"] == "ok"
        assert lp["closure_test"]["mahalanobis_sq"] < 1e-6

    def test_conflict_flagged_with_evidence_path(self):
        # 10 cm perturb, ~1.7 mm std => mahalanobis huge
        edges = self._triangle(perturb_t=(0.1, 0.0, 0.0))
        r = audit_loops(
            _req(edges, loops=[LoopSpec(name="L", frame_ids=["a", "b", "c", "a"])])
        )
        assert r["summary"]["status"] == "conflict"
        f = next(x for x in r["findings"] if x["kind"] == "loop_closure_conflict")
        assert f["evidence_path"] == ["a", "b", "c", "a"]
        assert f["mahalanobis_sq"] > 12.592

    def test_shortest_conflict_path_selected(self):
        # Two explicit loops that both conflict. The shortest (3-edge
        # triangle) must be reported, not the 4-edge quadrilateral.
        e0 = edge("a", "b", translation=[1, 0, 0], rotation=np.eye(3), trans_std=1e-3, rot_std=1e-3, edge_id="e0")
        e1 = edge("b", "c", translation=[0, 1, 0], rotation=np.eye(3), trans_std=1e-3, rot_std=1e-3, edge_id="e1")
        # triangle c->a, perturbed => triangle a-b-c-a conflicts
        e2 = edge("a", "c", translation=[-1.0, -0.95, 0.0], rotation=np.eye(3),
                  trans_std=1e-3, rot_std=1e-3, edge_id="e2")
        e3 = edge("c", "d", translation=[0, 0, 1], rotation=np.eye(3), trans_std=1e-3, rot_std=1e-3, edge_id="e3")
        e4 = edge("a", "d", translation=[-1.0, -1.0, 0.9], rotation=np.eye(3),
                  trans_std=1e-3, rot_std=1e-3, edge_id="e4")
        r = audit_loops(
            _req(
                [e0, e1, e2, e3, e4],
                loops=[
                    LoopSpec(name="quad", frame_ids=["a", "b", "c", "d", "a"]),
                    LoopSpec(name="triangle", frame_ids=["a", "b", "c", "a"]),
                ],
            )
        )
        assert r["summary"]["conflicting_loops"] >= 1
        assert r["summary"]["shortest_conflict_loop"] == "triangle"
        assert r["summary"]["shortest_conflict_path"] == ["a", "b", "c", "a"]

    def test_reverse_traversal_direction_recorded(self):
        edges = self._triangle()
        r = audit_loops(
            _req(edges, loops=[LoopSpec(name="L", frame_ids=["a", "c", "b", "a"])])
        )
        dirs = [(e["edge_id"], e["traversed_as"]) for e in r["loops"][0]["edges"]]
        # walk a->c (e2: child c->parent a, so reversed),
        # c->b (e1: child c->parent b, forward), b->a (e0: forward).
        assert [d[0] for d in dirs] == ["e2", "e1", "e0"]
        assert dirs[0][1].startswith("reverse")
        assert dirs[1][1].startswith("forward")
        assert dirs[2][1].startswith("forward")


class TestOpenChain:
    def test_open_chain_composition_and_covariance(self):
        e0 = edge("a", "b", translation=[1, 0, 0], edge_id="e0")
        e1 = rotated_edge_z("b", "c", np.pi / 2, translation=[0, 1, 0], edge_id="e1")
        req = ChainRequest(
            calibration_version="v",
            correlation_policy="independent",
            edges=[e0, e1],
            frame_path=["a", "b", "c"],
        )
        r = audit_chain(req)
        assert r["summary"]["status"] == "propagated"
        assert r["covariance"] is not None
        assert len(r["covariance"]) == 6

    def test_missing_frame_rejected(self):
        e0 = edge("a", "b")
        req = ChainRequest(
            calibration_version="v",
            correlation_policy="independent",
            edges=[e0],
            frame_path=["a", "zzz"],
        )
        r = audit_chain(req)
        assert r["summary"]["status"] == "rejected"
        assert any(x["kind"] == "no_edge_between_frames" for x in r["findings"])


class TestMissingCovariance:
    def test_missing_covariance_indeterminate_not_zero(self):
        e0 = edge("a", "b", edge_id="known")
        e1 = edge("b", "c", edge_id="unknown", cov=None)
        req = ChainRequest(
            calibration_version="v",
            correlation_policy="independent",
            edges=[e0, e1],
            frame_path=["a", "b", "c"],
        )
        r = audit_chain(req)
        assert r["summary"]["status"] == "indeterminate"
        assert r["covariance"] is None
        f = next(x for x in r["findings"] if x["kind"] == "missing_covariance")
        assert "unknown" in f["edge_ids"]
        assert "NOT treated as zero" in f["message"]

    def test_loop_with_missing_covariance_indeterminate_closure(self):
        e0 = edge("a", "b", edge_id="e0")
        e1 = edge("b", "c", edge_id="e1", cov=None)
        e2 = edge("a", "c", edge_id="e2")
        r = audit_loops(
            _req([e0, e1, e2], loops=[
                LoopSpec(name="L", frame_ids=["a", "b", "c", "a"])
            ])
        )
        assert r["loops"][0]["closure_test"]["status"] == "indeterminate"
        assert "e1" in r["loops"][0]["closure_test"]["missing_edges"]
