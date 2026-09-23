"""
Acceptance tests for the inverse-kinematics service.

Covers:
  * forward kinematics (independent matrix reference) and Jacobian
    (finite differences)
  * known-FK targets solved and independently FK-verified
  * multi-solution branch selection by *periodic* distance to current pose
  * fully extended straight/singular pose (exactly reachable -> verified ok;
    perturbed just beyond reach -> singular_no_convergence)
  * out-of-workspace target -> unreachable
  * joint-limit conflict -> joint_limit_conflict
  * initial-guess variation (different seeds still verify)
  * periodic angular distance is never a plain subtraction
  * no success can ever be returned without a passed FK verification
  * HTTP API: HMAC-SHA256 auth (valid / missing / bad / replay),
    validation, end-to-end signed solve
"""

from __future__ import annotations

import hashlib
import math
import time

import numpy as np
import pytest
from fastapi.testclient import TestClient

from app import robot
from app.auth import sign
from app.main import app

client = TestClient(app)

KEY_ID = "demo-key-id"
SECRET = b"demo-secret"
SOLVE_PATH = "/api/v1/ik/solve"


# --------------------------------------------------------------------------
# Fixtures / helpers
# --------------------------------------------------------------------------

def rng() -> np.random.Generator:
    return np.random.default_rng(20260923)


def random_inside(r: np.random.Generator) -> np.ndarray:
    return robot.clamp_to_limits(
        (r.random(6) * 2 - 1) * np.deg2rad([150, 100, 120, 250, 90, 250]))


def _mdh(a, al, d, th):
    ca, sa = math.cos(al), math.sin(al)
    ct, st = math.cos(th), math.sin(th)
    return np.array([
        [ct, -st, 0.0, a],
        [st * ca, ct * ca, -sa, -sa * d],
        [st * sa, ct * sa, ca, ca * d],
        [0.0, 0.0, 0.0, 1.0],
    ])


def fk_independent(q):
    """Reference FK built directly from 4x4 DH matrices (second code path)."""
    T = np.eye(4)
    for i in range(6):
        T = T @ _mdh(robot.A_MDH[i], robot.ALPHA_MDH[i],
                     robot.D_MDH[i], q[i])
    return T[:3, :3], T[:3, 3]


def signed_post(body: dict, key_id: str = KEY_ID, secret: bytes = SECRET,
                timestamp: float | None = None, path: str = SOLVE_PATH):
    raw = __import__("json").dumps(body).encode()
    ts = str(int(timestamp if timestamp is not None else time.time()))
    sig = sign("POST", path, ts, raw, secret)
    return client.post(path, content=raw, headers={
        "Content-Type": "application/json",
        "X-Key": key_id,
        "X-Timestamp": ts,
        "X-Signature": sig,
    })


def target_body(R, p, current=None):
    return {
        "position": [float(x) for x in p],
        "rotation": [[float(x) for x in row] for row in R],
        "current_joints": None if current is None
        else [float(x) for x in current],
    }


# --------------------------------------------------------------------------
# Kinematics correctness
# --------------------------------------------------------------------------

def test_fk_matches_independent_reference():
    r = rng()
    for _ in range(25):
        q = random_inside(r)
        R1, p1 = robot.fk(q)
        R2, p2 = fk_independent(q)
        assert np.allclose(R1, R2, atol=1e-11)
        assert np.allclose(p1, p2, atol=1e-11)


def _numerical_jacobian(q, eps=1e-6):
    R0, p0 = robot.fk(q)
    J = np.zeros((6, 6))
    for i in range(6):
        dq = q.copy()
        dq[i] += eps
        Rd, pd = robot.fk(dq)
        e, _, _ = robot.pose_error(Rd, pd, R0, p0)
        J[:, i] = e / eps
    return J


def test_jacobian_matches_finite_differences():
    r = rng()
    for _ in range(6):
        q = (r.random(6) * 2 - 1) * np.deg2rad([120, 80, 100, 200, 80, 200])
        assert np.allclose(robot.geometric_jacobian(q),
                           _numerical_jacobian(q), atol=2e-5)


# --------------------------------------------------------------------------
# Known-FK targets and independent verification
# --------------------------------------------------------------------------

@pytest.mark.parametrize("k", range(20))
def test_known_fk_targets_roundtrip(k):
    r = np.random.default_rng(1000 + k)
    q0 = random_inside(r)
    R, p = robot.fk(q0)
    res = robot.solve_ik(R, p, current_q=q0, seed=k)
    assert res.status == "ok", res.reason
    # Independent FK re-check in the test itself (third code path usage)
    Rr, pr = fk_independent(res.q)
    _, pe, oe = robot.pose_error(R, p, Rr, pr)
    assert pe < robot.POS_TOL
    assert oe < robot.ORI_TOL
    assert res.verification is not None and res.verification.passed
    assert np.all(res.q >= robot.JOINT_LIMITS[:, 0])
    assert np.all(res.q <= robot.JOINT_LIMITS[:, 1])
    # Nearest branch: asking from q0 selects q0's own branch
    assert np.max(np.abs(robot.periodic_distance(res.q, q0))) < 5e-3


def test_success_cannot_be_returned_without_verification():
    """Corrupt the verification gate: solver must refuse to report ok."""
    r = rng()
    R, p = robot.fk(random_inside(r))

    real_verify = robot.verify_solution
    robot.verify_solution = lambda *a, **k: robot.Verification(
        1.0, 1.0, True, False)
    try:
        res = robot.solve_ik(R, p)
    finally:
        robot.verify_solution = real_verify
    assert res.status != "ok"
    assert res.q is None


# --------------------------------------------------------------------------
# Multi-solution selection uses periodic distance
# --------------------------------------------------------------------------

def test_periodic_distance_is_circular():
    assert abs(robot.periodic_distance(np.array([math.pi]),
                                      np.array([-math.pi]))[0]) < 1e-12
    assert abs(abs(robot.periodic_distance(np.array([3.1]),
                                          np.array([-3.1]))[0])
               - (2 * math.pi - 6.2)) < 1e-12
    w = float(robot.wrap_to_pi(np.asarray(3 * math.pi)))
    assert abs(abs(w) - math.pi) < 1e-12  # +pi and -pi are the same point
    # Plain subtraction would give 2*pi here; periodic distance gives ~0
    assert abs(robot.periodic_distance(np.array([2 * math.pi]),
                                      np.array([0.0]))[0]) < 1e-12


def test_branch_changes_with_reference_pose():
    """Pick a target with two in-limit shoulder branches; ask the solver
    from each branch's own configuration and assert it returns that
    branch. Also a different reference must choose a different result."""
    q0 = robot.clamp_to_limits(
        np.deg2rad([37.0, 30.0, -2.2, 77.2, -38.2, -98.1]))
    R, p = robot.fk(q0)
    base = robot.solve_ik(R, p, current_q=robot.HOME_Q)
    assert base.status == "ok"
    branch_q1 = sorted({round(c["joint_angles_deg"][0], 1)
                        for c in base.candidates})
    assert any(v < -30 for v in branch_q1) and any(v > 30 for v in branch_q1)

    for c in base.candidates:
        cur = np.deg2rad(c["joint_angles_deg"])
        r = robot.solve_ik(R, p, current_q=cur)
        assert r.status == "ok"
        d = np.max(np.abs(robot.periodic_distance(r.q, cur)))
        assert d < 1e-2, (d, c["joint_angles_deg"],
                          np.rad2deg(r.q).tolist())

    # home and q0 pick the closest branch; it need not equal q0's branch
    home = robot.solve_ik(R, p, current_q=robot.HOME_Q)
    near = robot.solve_ik(R, p, current_q=q0)
    assert home.status == near.status == "ok"
    assert near.candidates[0]["periodic_distance_to_current_rad"] < 1e-3
    assert len(base.candidates) >= 2


def test_weights_affect_selection():
    """A fixture where weighting joint 1 heavily flips the chosen branch."""
    q0 = robot.clamp_to_limits(
        np.deg2rad([-59.4, 45.5, 68.3, -181.6, -13.0, 157.6]))
    R, p = robot.fk(q0)
    plain = robot.solve_ik(R, p, current_q=robot.HOME_Q,
                           weights=np.ones(6))
    heavy = robot.solve_ik(R, p, current_q=robot.HOME_Q,
                           weights=np.array([5.0, 1, 1, 1, 1, 1]))
    assert plain.status == heavy.status == "ok"
    assert abs(plain.q[0] - heavy.q[0]) > math.radians(20)
    assert abs(heavy.q[0]) < abs(plain.q[0])


# --------------------------------------------------------------------------
# Singular / unreachable / limit-conflict separation
# --------------------------------------------------------------------------

STRAIGHT_Q = np.array([0.0, 0.0, -math.pi / 2, 0.0, 0.0, 0.0])


def test_straight_singular_pose_exactly_reachable():
    R, p = robot.fk(STRAIGHT_Q)
    smin = np.linalg.svd(robot.geometric_jacobian(STRAIGHT_Q),
                         compute_uv=False)[-1]
    assert smin < 1e-10  # the fixture really is singular
    res = robot.solve_ik(R, p)
    assert res.status == "ok"
    assert res.verification.passed  # exact singular target still verifies


def test_near_singular_target_does_not_converge():
    """Push the straight-singular target a few mm radially outward beyond
    the maximum wrist-centre radius but inside the TCP precheck envelope:
    the residual lies along the singular direction and DLS stalls."""
    R, p = robot.fk(STRAIGHT_Q)
    p_out = p.copy()
    rho = math.hypot(p[0], p[1])
    f = 1.0 + 0.005 / rho
    p_out[0] *= f
    p_out[1] *= f
    assert math.hypot(*p_out[:2]) < robot.REACH_MAX  # not the coarse gate
    res = robot.solve_ik(R, p_out)
    assert res.status == "singular_no_convergence", res.status
    assert res.q is None
    best = min(res.runs, key=lambda r: r.position_error + r.orientation_error)
    assert best.min_singular_value < robot.SINGULAR_SMIN


def test_unreachable_target():
    res = robot.solve_ik(np.eye(3), np.array([3.0, 0.0, 0.2]))
    assert res.status == "unreachable"
    assert res.q is None
    res2 = robot.solve_ik(np.eye(3), np.array([0.0, 0.0, -5.0]))
    assert res2.status == "unreachable"


def test_joint_limit_conflict():
    # Base azimuth 181 deg forces q1 past its +/-160 deg limit on every
    # feasible branch; iterations end clamped at the limit with a residual.
    a = math.radians(181.0)
    p = np.array([0.45 * math.cos(a), 0.45 * math.sin(a), 0.10])
    res = robot.solve_ik(np.eye(3), p)
    assert res.status == "joint_limit_conflict", res.status
    assert res.q is None
    best = min(res.runs, key=lambda r: r.position_error + r.orientation_error)
    assert best.clamp_fraction >= robot.CLAMP_FRACTION_LIMIT


def test_three_failure_classes_are_distinct():
    classes = []
    classes.append(robot.solve_ik(np.eye(3),
                                  np.array([3.0, 0.0, 0.2])).status)
    R, p = robot.fk(STRAIGHT_Q)
    po = p.copy()
    f = 1 + 0.005 / math.hypot(p[0], p[1])
    po[0] *= f
    po[1] *= f
    classes.append(robot.solve_ik(R, po).status)
    a = math.radians(181.0)
    classes.append(robot.solve_ik(
        np.eye(3),
        np.array([0.45 * math.cos(a), 0.45 * math.sin(a), 0.10])).status)
    assert classes == ["unreachable", "singular_no_convergence",
                       "joint_limit_conflict"]


# --------------------------------------------------------------------------
# Initial-value variation
# --------------------------------------------------------------------------

@pytest.mark.parametrize("seed", [1, 7, 42, 123456])
def test_different_initial_value_seeds_still_verify(seed):
    r = np.random.default_rng(seed)
    R, p = robot.fk(random_inside(r))
    res = robot.solve_ik(R, p, current_q=robot.HOME_Q, seed=seed)
    assert res.status == "ok"
    Rr, pr = fk_independent(res.q)
    _, pe, oe = robot.pose_error(R, p, Rr, pr)
    assert pe < robot.POS_TOL and oe < robot.ORI_TOL
    assert res.verification.passed


def test_multiple_initial_guesses_are_actually_used():
    r = rng()
    R, p = robot.fk(random_inside(r))
    res = robot.solve_ik(R, p)
    assert res.as_dict(None, np.ones(6))["num_initial_guesses"] >= 4
    assert len({tuple(np.round(run.seed, 4)) for run in res.runs}) == \
        len(res.runs)


# --------------------------------------------------------------------------
# HTTP API + HMAC crypto
# --------------------------------------------------------------------------

def test_health():
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


def test_signed_solve_end_to_end():
    r = rng()
    q0 = random_inside(r)
    R, p = robot.fk(q0)
    resp = signed_post(target_body(R, p, current=q0))
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["status"] == "ok"
    assert data["verification"]["passed"] is True
    assert data["verification"]["position_error_m"] < robot.POS_TOL
    q = np.array(data["joint_angles_rad"])
    Rr, pr = fk_independent(q)
    _, pe, _ = robot.pose_error(R, p, Rr, pr)
    assert pe < robot.POS_TOL


def test_signed_solve_singular_via_api():
    R, p = robot.fk(STRAIGHT_Q)
    po = p.copy()
    f = 1 + 0.005 / math.hypot(p[0], p[1])
    po[0] *= f
    po[1] *= f
    resp = signed_post(target_body(R, po))
    assert resp.json()["status"] == "singular_no_convergence"


def test_missing_signature_headers_rejected():
    body = target_body(np.eye(3), np.array([0.4, 0.0, 0.2]))
    resp = client.post(SOLVE_PATH, json=body)
    assert resp.status_code == 401


def test_bad_signature_rejected():
    body = target_body(np.eye(3), np.array([0.4, 0.0, 0.2]))
    import json
    raw = json.dumps(body).encode()
    resp = client.post(SOLVE_PATH, content=raw, headers={
        "Content-Type": "application/json",
        "X-Key": KEY_ID,
        "X-Timestamp": str(int(time.time())),
        "X-Signature": "00" * 32,
    })
    assert resp.status_code == 401
    assert "signature mismatch" in resp.json()["detail"]


def test_unknown_key_rejected():
    body = target_body(np.eye(3), np.array([0.4, 0.0, 0.2]))
    resp = signed_post(body, key_id="nobody", secret=b"x")
    # signature computed with wrong secret AND unknown id -> 401
    assert resp.status_code == 401


def test_stale_timestamp_rejected_replay_protection():
    body = target_body(np.eye(3), np.array([0.4, 0.0, 0.2]))
    resp = signed_post(body, timestamp=time.time() - 400)
    assert resp.status_code == 401
    assert "skew" in resp.json()["detail"]


def test_signature_is_real_hmac_sha256():
    # Independently recompute what the client sends and compare to a
    # from-scratch hmac implementation in the test.
    import hmac as _hmac
    body = target_body(np.eye(3), np.array([0.4, 0.0, 0.2]))
    import json
    raw = json.dumps(body).encode()
    ts = str(int(time.time()))
    canonical = b"\n".join([
        b"POST", SOLVE_PATH.encode(), ts.encode(),
        hashlib.sha256(raw).hexdigest().encode()])
    expected = _hmac.new(SECRET, canonical, hashlib.sha256).hexdigest()
    assert sign("POST", SOLVE_PATH, ts, raw, SECRET) == expected


def test_invalid_rotation_matrix_rejected():
    body = {"position": [0.4, 0.0, 0.2],
            "rotation": [[1.0, 0.0, 0.0],
                         [0.0, 1.0, 0.0],
                         [0.0, 0.0, -1.0]]}  # det -1 (reflection)
    resp = signed_post(body)
    assert resp.status_code == 422


def test_body_tampering_invalidates_signature():
    import json
    body = target_body(np.eye(3), np.array([0.4, 0.0, 0.2]))
    raw = json.dumps(body).encode()
    ts = str(int(time.time()))
    sig = sign("POST", SOLVE_PATH, ts, raw, SECRET)
    tampered = raw.replace(b"0.4", b"0.5")
    resp = client.post(SOLVE_PATH, content=tampered, headers={
        "Content-Type": "application/json",
        "X-Key": KEY_ID, "X-Timestamp": ts, "X-Signature": sig})
    assert resp.status_code == 401
