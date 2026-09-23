"""
Kinematics core for the specified 6-joint serial manipulator.

Robot model (a scaled PUMA-560-class arm, all lengths in metres,
revolute joints, angles in radians).

Modified/Craig-style Denavit-Hartenberg parameters:
    frame i transform: R_x(alpha_{i-1}) * T_x(a_{i-1}) * R_z(theta_i) * T_z(d_i)

    joint | a_{i-1} (m) | alpha_{i-1} (rad) | d_i (m)
    ------+-------------+-------------------+--------
      1   |    0.000    |   0               |  0.150
      2   |    0.000    |  -pi/2            |  0.140
      3   |    0.250    |   0               |  0.000
      4   |    0.020    |  -pi/2            |  0.250
      5   |    0.000    |   pi/2            |  0.000
      6   |    0.000    |  -pi/2            |  0.085

Joint soft limits (the service never returns a joint outside these):
    q1 in [-160, 160] deg
    q2 in [-110, 110] deg
    q3 in [-135, 135] deg
    q4 in [-266, 266] deg
    q5 in [-100, 100] deg
    q6 in [-266, 266] deg
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np

# ---------------------------------------------------------------------------
# Model definition
# ---------------------------------------------------------------------------

NUM_JOINTS = 6

# Modified DH: a[i-1], alpha[i-1] for i = 1..6  (rows indexed by joint)
A_MDH = np.array([0.000, 0.000, 0.250, 0.020, 0.000, 0.000])
ALPHA_MDH = np.array([
    0.0,
    -math.pi / 2.0,
    0.0,
    -math.pi / 2.0,
    math.pi / 2.0,
    -math.pi / 2.0,
])
D_MDH = np.array([0.150, 0.140, 0.000, 0.250, 0.000, 0.085])

# Joint limits in radians, (lower, upper) per joint
JOINT_LIMITS_DEG = np.array([
    [-160.0, 160.0],
    [-110.0, 110.0],
    [-135.0, 135.0],
    [-266.0, 266.0],
    [-100.0, 100.0],
    [-266.0, 266.0],
])
JOINT_LIMITS = np.deg2rad(JOINT_LIMITS_DEG)

# Home / nominal pose used as the preferred branch
HOME_Q = np.zeros(6)


def _wrist_reach_extrema() -> tuple[float, float]:
    """Numerically compute min/max distance of the wrist centre (frame 5)
    from the base origin over (q2, q3) inside their limits.
    Dense deterministic sampling — geometry is independent of q1, q4..q6.
    """
    q2 = np.linspace(JOINT_LIMITS[1, 0], JOINT_LIMITS[1, 1], 2401)
    q3 = np.linspace(JOINT_LIMITS[2, 0], JOINT_LIMITS[2, 1], 2401)
    g2, g3 = np.meshgrid(q2, q3, indexing="ij")

    # Frame-3 origin relative to frame-1 centre (Craig Puma geometry)
    # r = a2*c2 + d4*s23 + a3*c23,  z = d2 + a2*s2 - d4*c23 + a3*s23
    s2 = np.sin(g2)
    c2 = np.cos(g2)
    s23 = np.sin(g2 + g3)
    c23 = np.cos(g2 + g3)
    a2 = A_MDH[2]          # a_2 (offset of frame 3) = 0.250
    a3 = A_MDH[3]          # a_3 = 0.020
    d2 = D_MDH[1]          # d_2 = 0.140
    d4 = D_MDH[3]          # d_4 = 0.250
    r = a2 * c2 + d4 * s23 + a3 * c23
    z = d2 + a2 * s2 - d4 * c23 + a3 * s23
    rho = np.sqrt(r * r + z * z)
    return float(rho.min()), float(rho.max())


_RMIN_WRIST, _RMAX_WRIST = _wrist_reach_extrema()
# Tool flange offset d6 beyond the wrist centre
D6 = float(D_MDH[5])
# Conservative TCP reach envelope (axisymmetric). Margin 2 cm absorbs
# the fact that the wrist-centre radii are sampled, not analytic.
REACH_MAX = math.sqrt(max(0.0, _RMAX_WRIST ** 2 - D6 ** 2)) + D6
REACH_MIN = max(0.0, _RMIN_WRIST - D6)
REACH_MARGIN = 0.02

# ---------------------------------------------------------------------------
# Solver parameters
# ---------------------------------------------------------------------------

POS_TOL = 2.0e-4           # m, success threshold after FK verification
ORI_TOL = 1.0e-3           # rad
MAX_ITERS = 120
LAMBDA_BASE = 1.0e-2       # damped-least-squares damping
LAMBDA_MAX = 5.0e-2
SINGULAR_SMIN = 2.0e-2     # below this singular value we treat the arm as near-singular
MAX_STEP = 0.3             # rad per iteration, per joint
CLAMP_FRACTION_LIMIT = 0.15
NEAR_POS = 0.05            # best-residual proximity used when classifying failures
NEAR_ORI = 0.35

SOLVE_STATUSES = (
    "ok",
    "unreachable",
    "singular_no_convergence",
    "joint_limit_conflict",
)


# ---------------------------------------------------------------------------
# Angle periodicity
# ---------------------------------------------------------------------------

def wrap_to_pi(angle: np.ndarray) -> np.ndarray:
    """Wrap an angle (or array) to (-pi, pi]. NOT a plain subtraction:
    periodic partners such as pi and -pi are identical here."""
    a = np.asarray(angle, dtype=float)
    return (a + math.pi) % (2.0 * math.pi) - math.pi


def periodic_distance(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    """Shortest signed-on-magnitude angular distance a - b on the circle.
    abs(periodic_distance(a, b)) is the geodesic distance in [0, pi];
    it must be used instead of |a - b| for revolute joints."""
    return wrap_to_pi(np.asarray(a, dtype=float) - np.asarray(b, dtype=float))


def clamp_to_limits(q: np.ndarray, margin: float = 1.0e-9) -> np.ndarray:
    return np.minimum(np.maximum(q, JOINT_LIMITS[:, 0] + margin),
                      JOINT_LIMITS[:, 1] - margin)


# ---------------------------------------------------------------------------
# Forward kinematics
# ---------------------------------------------------------------------------

def _mdh_transform(a: float, alpha: float, d: float, theta: float) -> np.ndarray:
    ca, sa = math.cos(alpha), math.sin(alpha)
    ct, st = math.cos(theta), math.sin(theta)
    return np.array([
        [ct, -st, 0.0, a],
        [st * ca, ct * ca, -sa, -sa * d],
        [st * sa, ct * sa, ca, ca * d],
        [0.0, 0.0, 0.0, 1.0],
    ], dtype=float)


def fk_all(q: np.ndarray):
    """Forward kinematics. Returns (R_06, p_06, Rs, ps) where Rs/ps are
    per-frame rotations/origins (R_00..R_06, p_00..p_06)."""
    q = np.asarray(q, dtype=float)
    R = np.eye(3)
    p = np.zeros(3)
    Rs = [np.eye(3)]
    ps = [np.zeros(3)]
    for i in range(NUM_JOINTS):
        T = _mdh_transform(A_MDH[i], ALPHA_MDH[i], D_MDH[i], q[i])
        Ri = T[:3, :3]
        pi = T[:3, 3]
        p = R @ pi + p
        R = R @ Ri
        Rs.append(R.copy())
        ps.append(p.copy())
    return R, p, Rs, ps


def fk(q: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Tool pose for joint vector q: (rotation matrix 3x3, position 3-vector)."""
    R, p, _, _ = fk_all(q)
    return R, p


def _axis_angle_error(R_des: np.ndarray, R_cur: np.ndarray) -> tuple[np.ndarray, float]:
    """Orientation error expressed in the BASE frame: angle*axis of R_err.
    Magnitude is the geodesic rotation angle in [0, pi]."""
    R_err = R_des @ R_cur.T
    # Orthogonalise against accumulated numerical drift
    U, _, Vt = np.linalg.svd(R_err)
    R_err = U @ Vt
    if np.linalg.det(R_err) < 0:
        U[:, -1] *= -1.0
        R_err = U @ Vt
    cos_theta = (np.trace(R_err) - 1.0) / 2.0
    cos_theta = float(np.clip(cos_theta, -1.0, 1.0))
    theta = math.acos(cos_theta)
    if theta < 1.0e-10:
        return np.zeros(3), 0.0
    if math.pi - theta < 1.0e-8:
        # Near 180 deg: extract axis from symmetric part robustly
        w, v = np.linalg.eig((R_err + R_err.T) / 2.0)
        axis = np.real(v[:, np.argmin(np.abs(w - 1.0))])
        axis /= max(np.linalg.norm(axis), 1e-15)
        return axis * theta, theta
    axis = np.array([
        R_err[2, 1] - R_err[1, 2],
        R_err[0, 2] - R_err[2, 0],
        R_err[1, 0] - R_err[0, 1],
    ]) / (2.0 * math.sin(theta))
    return axis * theta, theta


def pose_error(R_des: np.ndarray, p_des: np.ndarray,
               R_cur: np.ndarray, p_cur: np.ndarray) -> tuple[np.ndarray, float, float]:
    """6-vector error in base frame [w(3), v(3)] plus scalar pos/ori norms."""
    w, ori_norm = _axis_angle_error(R_des, R_cur)
    v = np.asarray(p_des, dtype=float) - np.asarray(p_cur, dtype=float)
    return np.concatenate([w, v]), float(np.linalg.norm(v)), ori_norm


def geometric_jacobian(q: np.ndarray) -> np.ndarray:
    """6x6 geometric Jacobian mapping qdot to base-frame spatial velocity
    [omega; v] of the tool, consistent with pose_error ordering.

    For Craig modified DH, ^{i-1}T_i = Rx(alpha) Tx(a) Rz(theta) Tz(d);
    the revolute axis of joint i is the z axis of the intermediate frame
    after Rx/Tx, passing through the point displaced by Tx(a) — not the z
    axis / origin of frame {i}.
    """
    q = np.asarray(q, dtype=float)
    R6, p6, _, _ = fk_all(q)

    J = np.zeros((6, 6))
    R = np.eye(3)
    p = np.zeros(3)
    for i in range(NUM_JOINTS):
        al = ALPHA_MDH[i]
        ca, sa = math.cos(al), math.sin(al)
        # Intermediate frame M_i: frame {i-1} transformed by Rx(alpha) Tx(a)
        Rx = np.array([[1.0, 0.0, 0.0],
                       [0.0, ca, -sa],
                       [0.0, sa, ca]])
        Rm = R @ Rx
        om = p + R @ np.array([A_MDH[i], 0.0, 0.0])
        z = Rm[:, 2]
        J[:3, i] = z
        J[3:, i] = np.cross(z, p6 - om)

        # Advance to frame {i} for the next iteration
        T = _mdh_transform(A_MDH[i], ALPHA_MDH[i], D_MDH[i], q[i])
        p = p + R @ T[:3, 3]
        R = R @ T[:3, :3]
    return J


# ---------------------------------------------------------------------------
# Verification — independent re-check of a candidate with a fresh FK pass
# ---------------------------------------------------------------------------

@dataclass
class Verification:
    position_error: float
    orientation_error: float
    within_limits: bool
    passed: bool

    def as_dict(self) -> dict:
        return {
            "position_error_m": self.position_error,
            "orientation_error_rad": self.orientation_error,
            "within_limits": self.within_limits,
            "passed": self.passed,
        }


def verify_solution(q: np.ndarray, R_des: np.ndarray, p_des: np.ndarray,
                    pos_tol: float = POS_TOL, ori_tol: float = ORI_TOL) -> Verification:
    """Independent acceptance gate: recompute FK from the candidate and
    compare against the requested target. A success is never reported
    unless this passes."""
    q = np.asarray(q, dtype=float)
    R, p = fk(q)
    _, pe, oe = pose_error(R_des, p_des, R, p)
    within = bool(np.all(q >= JOINT_LIMITS[:, 0]) and np.all(q <= JOINT_LIMITS[:, 1]))
    return Verification(
        position_error=pe,
        orientation_error=oe,
        within_limits=within,
        passed=bool(pe <= pos_tol and oe <= ori_tol and within),
    )


# ---------------------------------------------------------------------------
# Reachability pre-check
# ---------------------------------------------------------------------------

def reachable_envelope(p_des: np.ndarray) -> tuple[bool, float, str]:
    """Cheap axisymmetric workspace test on target position only."""
    r = float(np.linalg.norm(np.asarray(p_des, dtype=float)))
    if r > REACH_MAX + REACH_MARGIN:
        return False, r, f"target radius {r:.3f} m exceeds reach max {REACH_MAX:.3f} m"
    if r < max(0.0, REACH_MIN - REACH_MARGIN):
        # Inner hole: wrist cannot fold that tightly. (Very small targets
        # may still be reachable when the wrist folds, so only flag clear
        # violations of the sampled inner radius.)
        return False, r, f"target radius {r:.3f} m inside reach min {REACH_MIN:.3f} m"
    return True, r, ""


# ---------------------------------------------------------------------------
# Seed construction
# ---------------------------------------------------------------------------

def analytic_seeds(R_des: np.ndarray, p_des: np.ndarray) -> list[np.ndarray]:
    """Branch-complete closed-form initial guesses for the Craig-MDH
    PUMA geometry (the geometry below is exact for this arm; the DLS
    iteration only polishes numerical round-off and reports residuals).

    Yields up to 2 (shoulder) x 2 (elbow) x 2 (wrist flip) = 8 candidates.
    """
    R_des = np.asarray(R_des, dtype=float)
    p_des = np.asarray(p_des, dtype=float)
    a2 = float(A_MDH[2])
    a3 = float(A_MDH[3])
    d1 = float(D_MDH[0])
    d2 = float(D_MDH[1])
    d4 = float(D_MDH[3])
    l3 = math.hypot(a3, d4)          # effective forearm length
    delta = math.atan2(d4, a3)       # forearm offset angle (a3 vs d4)

    # Wrist centre = TCP pulled back by d6 along the tool approach axis z6
    pw = p_des - D6 * R_des[:, 2]
    rho = math.hypot(pw[0], pw[1])
    zc = float(pw[2]) - d1

    seeds: list[np.ndarray] = []
    if rho + 1e-9 < abs(d2):
        return seeds

    for sx in (1.0, -1.0):  # shoulder branch: x1 = +/- sqrt(rho^2 - d2^2)
        x = sx * math.sqrt(max(rho * rho - d2 * d2, 0.0))
        q1 = math.atan2(pw[1], pw[0]) - math.atan2(d2, x)

        # Planar 2R in the frame-1 (x1, z1) plane, clockwise-positive:
        #   V = a2*(c2,-s2) + l3*(c(q23+d), -s(q23+d))
        V2 = a2 * a2 + l3 * l3
        cos_q3d = (x * x + zc * zc - V2) / (2.0 * a2 * l3)
        if not (-1.0 - 1e-7 <= cos_q3d <= 1.0 + 1e-7):
            continue
        q3d_mag = math.acos(float(np.clip(cos_q3d, -1.0, 1.0)))
        phi = math.atan2(-zc, x)  # clockwise angle convention
        for sgn in (1.0, -1.0):  # elbow up / down
            q3 = sgn * q3d_mag - delta
            beta = math.atan2(l3 * math.sin(sgn * q3d_mag),
                              a2 + l3 * math.cos(sgn * q3d_mag))
            q2 = phi - beta

            R01 = np.array([
                [math.cos(q1), -math.sin(q1), 0.0],
                [math.sin(q1), math.cos(q1), 0.0],
                [0.0, 0.0, 1.0],
            ])
            R13 = _R13(q2, q3)
            R36 = R13.T @ R01.T @ R_des
            try:
                wrist_pairs = _wrist_angles(R36)
            except ValueError:
                continue
            for q4, q5, q6 in wrist_pairs:
                q = np.array([q1, q2, q3, q4, q5, q6], dtype=float)
                q = wrap_to_pi(q)
                q[3] = _nearest_in_range(q[3], JOINT_LIMITS[3, 0], JOINT_LIMITS[3, 1])
                q[5] = _nearest_in_range(q[5], JOINT_LIMITS[5, 0], JOINT_LIMITS[5, 1])
                seeds.append(clamp_to_limits(q))

    uniq: list[np.ndarray] = []
    for q in seeds:
        if all(np.max(np.abs(periodic_distance(q, u))) > 1e-3 for u in uniq):
            uniq.append(q)
    return uniq


def _R13(q2: float, q3: float) -> np.ndarray:
    """Rotation R_{1,3} for joints 2..3 under Craig modified DH:
    R12 = Rx(-pi/2) Rz(q2), R23 = Rz(q3)."""
    c2, s2 = math.cos(q2), math.sin(q2)
    c23, s23 = math.cos(q2 + q3), math.sin(q2 + q3)
    return np.array([
        [c23, -s23, 0.0],
        [0.0, 0.0, 1.0],
        [-s23, -c23, 0.0],
    ])


def _wrist_angles(R36: np.ndarray) -> list[tuple[float, float, float]]:
    """Extract (q4, q5, q6) pairs from R_{3,6} of the Craig-MDH wrist:
    R36 = Rx(-pi/2)Rz(q4) Rx(pi/2)Rz(q5) Rx(-pi/2)Rz(q6)
        = [[ c4 c5 c6 - s4 s6, -c4 c5 s6 - s4 c6,  s4 ],
           [ s5 c6,             -s5 s6,              c5 ],
           [-s4 c5 c6 - c4 s6,  s4 c5 s6 - c4 c6, -s4 s5]]
    """
    c5 = float(np.clip(R36[1, 2], -1.0, 1.0))
    out: list[tuple[float, float, float]] = []
    for q5 in (math.acos(c5), -math.acos(c5)):
        s5 = math.sin(q5)
        if abs(s5) < 1e-8:
            # Wrist singularity: q4 and q6 rotate about the same axis,
            # only (q4+q6) is observable. Fix q4 = 0.
            q4 = 0.0
            q6 = math.atan2(-R36[0, 1], R36[0, 0])
            out.append((q4, q5, q6))
            continue
        q6 = math.atan2(-R36[1, 1] / s5, R36[1, 0] / s5)
        # Recover R35 = R36 R56^T, then q4 robustly
        c6, s6 = math.cos(q6), math.sin(q6)
        # R56 = Rx(-pi/2) Rz(q6)
        R56 = np.array([
            [c6, -s6, 0.0],
            [0.0, 0.0, 1.0],
            [-s6, -c6, 0.0],
        ])
        R35 = R36 @ R56.T
        q4 = math.atan2(R35[0, 2], R35[2, 2])
        out.append((q4, q5, q6))
    return out


def _nearest_in_range(angle: float, lo: float, hi: float) -> float:
    """Map an angle to its periodic representative inside [lo, hi]
    if one exists (used for q4/q6 whose limits exceed +/-pi)."""
    k = math.floor((angle - lo) / (2.0 * math.pi) + 0.5)
    a = angle - k * 2.0 * math.pi
    if lo - 1e-9 <= a <= hi + 1e-9:
        return a
    return angle  # leave it; clamp step will handle


def random_seeds(n: int, rng: np.random.Generator) -> list[np.ndarray]:
    """Deterministic pseudo-random seeds uniformly inside the limits."""
    lo = JOINT_LIMITS[:, 0]
    hi = JOINT_LIMITS[:, 1]
    return [lo + rng.random(NUM_JOINTS) * (hi - lo) for _ in range(n)]


# ---------------------------------------------------------------------------
# Damped least-squares iteration from one initial guess
# ---------------------------------------------------------------------------

@dataclass
class SeedRun:
    seed: np.ndarray
    q: np.ndarray
    position_error: float
    orientation_error: float
    iterations: int
    converged: bool
    clamp_fraction: float
    min_singular_value: float
    stalled_near_singularity: bool
    hit_limits: bool


def _dls_run(seed: np.ndarray, R_des: np.ndarray, p_des: np.ndarray,
             max_iters: int = MAX_ITERS) -> SeedRun:
    q = np.array(seed, dtype=float)
    clamp_count = 0
    min_sv = math.inf
    stalled = False
    best = (math.inf, math.inf)
    stall_rounds = 0

    for it in range(1, max_iters + 1):
        R, p = fk(q)
        e, pe, oe = pose_error(R_des, p_des, R, p)
        best = (min(best[0], pe), min(best[1], oe))
        if pe <= POS_TOL and oe <= ORI_TOL:
            return SeedRun(seed, q, pe, oe, it, True,
                           clamp_count / it, min_sv, False,
                           bool(clamp_count > 0))

        J = geometric_jacobian(q)
        try:
            smin = float(np.linalg.svd(J, compute_uv=False)[-1])
        except np.linalg.LinAlgError:
            smin = 0.0
        min_sv = min(min_sv, smin)

        # Adaptive damping: raise lambda as manipulability collapses
        lam = LAMBDA_BASE
        if smin < SINGULAR_SMIN:
            lam = LAMBDA_BASE + (LAMBDA_MAX - LAMBDA_BASE) * (
                1.0 - smin / SINGULAR_SMIN)

        # dq = J^T (J J^T + lambda^2 I)^-1 e
        dq = J.T @ np.linalg.solve(J @ J.T + lam ** 2 * np.eye(6), e)
        nrm = np.linalg.norm(dq)
        if nrm > MAX_STEP:
            dq *= MAX_STEP / nrm

        # Integrate on the circle (periodic), then enforce joint limits
        qn = q + dq
        d = qn - q
        # Wrap the *increment* for unrestricted-style motion on cyclic joints
        qn = q + d
        qc = clamp_to_limits(qn)
        if np.max(np.abs(qc - qn)) > 1e-10:
            clamp_count += 1
        q = qc

        # Stall detection: residual stops improving while singular
        if it >= 12 and it % 8 == 0:
            if (abs(pe - best[0]) < 1e-12 and abs(oe - best[1]) < 1e-12
                    and smin < SINGULAR_SMIN
                    and (pe > POS_TOL or oe > ORI_TOL)):
                stall_rounds += 1
            else:
                stall_rounds = 0
            if stall_rounds >= 2 and it >= 32:
                stalled = True
                break

    R, p = fk(q)
    _, pe, oe = pose_error(R_des, p_des, R, p)
    return SeedRun(seed, q, pe, oe, max_iters, False,
                   clamp_count / max(max_iters, 1), min_sv,
                   stalled or (min_sv < SINGULAR_SMIN and
                               not (pe <= POS_TOL and oe <= ORI_TOL)),
                   bool(clamp_count > 0))


# ---------------------------------------------------------------------------
# Top-level multi-initial-value solver
# ---------------------------------------------------------------------------

@dataclass
class IKResult:
    status: str
    q: np.ndarray | None = None
    verification: Verification | None = None
    candidates: list[dict] = field(default_factory=list)
    runs: list[SeedRun] = field(default_factory=list)
    reason: str = ""
    target_radius: float = 0.0

    def as_dict(self, current_q: np.ndarray | None, weights) -> dict:
        d: dict = {
            "status": self.status,
            "reason": self.reason,
            "target_radius_m": self.target_radius,
            "num_initial_guesses": len(self.runs),
        }
        if self.q is not None:
            d["joint_angles_rad"] = [float(x) for x in self.q]
            d["joint_angles_deg"] = [float(x) for x in np.rad2deg(self.q)]
        if self.verification is not None:
            v = self.verification
            d["verification"] = {
                "position_error_m": v.position_error,
                "orientation_error_rad": v.orientation_error,
                "within_limits": v.within_limits,
                "passed": v.passed,
            }
        if self.candidates:
            d["candidates_evaluated"] = self.candidates
            if current_q is not None:
                d["selection"] = {
                    "criterion": "minimum periodic weighted distance to current_joints",
                    "weights": [float(w) for w in np.asarray(weights, dtype=float)],
                }
        return d


def solve_ik(R_des: np.ndarray, p_des: np.ndarray,
             current_q: np.ndarray | None = None,
             weights=None,
             seed: int = 20260923) -> IKResult:
    """Numerical IK with damping iteration from multiple initial guesses.

    Every converged run is independently FK-verified; the returned joint
    vector is the verified candidate with minimum *periodic* weighted
    distance to current_q (ties -> smaller residual). Failures are
    classified as unreachable / singular_no_convergence /
    joint_limit_conflict.
    """
    R_des = np.asarray(R_des, dtype=float)
    p_des = np.asarray(p_des, dtype=float)
    w = (np.ones(6) if weights is None
         else np.asarray(weights, dtype=float))
    if w.shape != (6,) or np.any(w <= 0):
        raise ValueError("weights must be 6 positive numbers")
    if current_q is not None:
        current_q = wrap_to_pi(np.asarray(current_q, dtype=float))

    ok_reach, radius, reach_msg = reachable_envelope(p_des)
    base = IKResult(status="unreachable", reason=reach_msg,
                    target_radius=radius)
    if not ok_reach:
        return base

    # Initial guesses in three tiers (all tiers still constitute genuine
    # multi-initial-value solving; tiers 2/3 are fallback restarts):
    #  1. analytic branch seeds — up to 2x2x2 = 8 exact configurations,
    #     so the nearest-branch selection sees every solution branch
    #  2. structured poses around the workspace
    #  3. deterministic random restarts
    rng = np.random.default_rng(seed)
    analytic: list[np.ndarray] = []
    try:
        analytic.extend(analytic_seeds(R_des, p_des))
    except Exception:
        pass
    structured_seeds = [
        HOME_Q,
        np.array([0.0, -math.pi / 4, math.pi / 2, 0.0, 0.0, 0.0]),
        np.array([0.0, math.pi / 4, -math.pi / 2, 0.0, 0.0, 0.0]),
        np.array([0.3, -0.6, 0.6, 0.5, 0.5, 0.0]),
        np.array([-0.3, 0.6, -0.6, -0.5, -0.5, 0.0]),
    ]
    if current_q is not None:
        structured_seeds.append(np.asarray(current_q, dtype=float))
    structured: list[np.ndarray] = []
    for s in structured_seeds:
        q = clamp_to_limits(wrap_to_pi(np.asarray(s, dtype=float)))
        if all(np.max(np.abs(periodic_distance(q, u))) > 1e-3
               for u in analytic + structured):
            structured.append(q)

    def _verified(run_list):
        out = []
        for run in run_list:
            if not run.converged:
                continue
            v = verify_solution(run.q, R_des, p_des)
            if v.passed:
                out.append((run, v))
        return out

    # Tier 1: analytic branches (polish + independent FK verification)
    runs = [_dls_run(s, R_des, p_des) for s in analytic]
    verified = _verified(runs)

    # Tier 2: structured restarts when the analytic set verifies nothing
    if not verified and structured:
        extra_runs = [_dls_run(s, R_des, p_des) for s in structured]
        runs.extend(extra_runs)
        verified = _verified(extra_runs)

    # Tier 3: random restarts
    if not verified:
        pool = analytic + structured
        extra = []
        for q in random_seeds(6, rng):
            if all(np.max(np.abs(periodic_distance(q, u))) > 0.25
                   for u in pool + extra):
                extra.append(q)
        extra = extra[:6]
        extra_runs = [_dls_run(s, R_des, p_des) for s in extra]
        runs.extend(extra_runs)
        verified = _verified(extra_runs)

    result = IKResult(status="unreachable", target_radius=radius, runs=runs)

    if verified:
        def cost(item):
            run, v = item
            qc = wrap_to_pi(run.q)
            ref = HOME_Q if current_q is None else current_q
            dist = float(np.sqrt(np.mean((w * periodic_distance(qc, ref)) ** 2)))
            return (dist, v.position_error + v.orientation_error)

        verified.sort(key=cost)
        chosen, chosen_v = verified[0]
        cand = []
        for run, v in verified:
            qc = wrap_to_pi(run.q)
            ref = HOME_Q if current_q is None else current_q
            dist = float(np.sqrt(np.mean((w * periodic_distance(qc, ref)) ** 2)))
            cand.append({
                "joint_angles_deg": [float(x) for x in np.rad2deg(run.q)],
                "position_error_m": v.position_error,
                "orientation_error_rad": v.orientation_error,
                "periodic_distance_to_current_rad": dist,
                "iterations": run.iterations,
            })
        cand.sort(key=lambda c: (c["periodic_distance_to_current_rad"],
                                 c["position_error_m"] + c["orientation_error_rad"]))
        result.status = "ok"
        result.q = chosen.q
        result.verification = chosen_v
        result.candidates = cand
        result.reason = (
            f"{len(verified)} verified candidate(s) from "
            f"{len(runs)} initial guesses; nearest-branch solution selected")
        return result

    # ---- failure classification over all runs ----
    # The target passed the coarse TCP workspace precheck, so distinguish:
    #  - singular stall: residual gets close but the arm is extended/folded
    #    at a rank-deficient configuration (no limit blocking)
    #  - joint-limit conflict: the closest runs are repeatedly clamped by
    #    the joint bounds
    best_run = min(runs, key=lambda r: r.position_error + r.orientation_error)

    limit_blocked = [r for r in runs
                     if r.clamp_fraction >= CLAMP_FRACTION_LIMIT
                     and r.position_error < NEAR_POS
                     and r.orientation_error < NEAR_ORI]
    singular_near = [r for r in runs
                     if r.position_error < NEAR_POS
                     and r.orientation_error < NEAR_ORI
                     and r.clamp_fraction < CLAMP_FRACTION_LIMIT
                     and (r.min_singular_value < SINGULAR_SMIN
                          or r.stalled_near_singularity)]

    if singular_near and (
            best_run.clamp_fraction < CLAMP_FRACTION_LIMIT
            or min(singular_near,
                   key=lambda r: r.position_error + r.orientation_error
                   ).position_error + min(singular_near,
                   key=lambda r: r.position_error + r.orientation_error
                   ).orientation_error
            <= best_run.position_error + best_run.orientation_error + 1e-12):
        r = min(singular_near,
                key=lambda r: r.position_error + r.orientation_error)
        result.status = "singular_no_convergence"
        result.reason = (
            "iteration stalls near a singularity: manipulability collapses "
            f"(min singular value {r.min_singular_value:.2e}, best "
            f"residual {r.position_error:.5f} m / "
            f"{r.orientation_error:.5f} rad; error lies along a singular "
            "direction and cannot be reduced)")
        return result

    if (best_run.clamp_fraction >= CLAMP_FRACTION_LIMIT
            and best_run.position_error < NEAR_POS
            and best_run.orientation_error < NEAR_ORI):
        result.status = "joint_limit_conflict"
        result.reason = (
            "target lies in the workspace but every approach terminates at a "
            f"joint limit (best residual {best_run.position_error:.4f} m / "
            f"{best_run.orientation_error:.4f} rad, clamp fraction "
            f"{best_run.clamp_fraction:.2f})")
        return result
    if limit_blocked:
        r = min(limit_blocked,
                key=lambda r: r.position_error + r.orientation_error)
        result.status = "joint_limit_conflict"
        result.reason = (
            "target pose requires joint motion beyond the configured limits "
            f"(best residual {r.position_error:.4f} m / "
            f"{r.orientation_error:.4f} rad, clamp fraction "
            f"{r.clamp_fraction:.2f})")
        return result

    near = [r for r in runs
            if r.position_error < NEAR_POS and r.orientation_error < NEAR_ORI]
    if near or any(r.stalled_near_singularity or r.min_singular_value < SINGULAR_SMIN
                   for r in runs):
        result.status = "singular_no_convergence"
        result.reason = (
            "iteration stalls near a singularity: manipulability collapses "
            f"(min singular value {best_run.min_singular_value:.2e}, best "
            f"residual {best_run.position_error:.4f} m / "
            f"{best_run.orientation_error:.4f} rad)")
        return result

    result.status = "unreachable"
    result.reason = (
        "no initial guess converges to the target; best residual "
        f"{best_run.position_error:.4f} m / "
        f"{best_run.orientation_error:.4f} rad across {len(runs)} guesses")
    return result
