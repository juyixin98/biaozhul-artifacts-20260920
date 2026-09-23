// Automated tests for the trajectory interpolation backend.
// Covers the acceptance criteria: rotations near 180 degrees, small angles,
// opposite-sign quaternions, unit-norm outputs, endpoint consistency, and
// data-gap rejection, plus the documented duplicate-timestamp and
// extrapolation policies.

#include <cmath>
#include <cstdio>
#include <stdexcept>
#include <string>

#include "../src/trajectory.hpp"

namespace {

int g_checks = 0;
int g_failures = 0;

void check(bool cond, const std::string& name) {
    ++g_checks;
    if (!cond) {
        ++g_failures;
        std::printf("FAIL: %s\n", name.c_str());
    }
}

bool near(double a, double b, double tol) { return std::fabs(a - b) <= tol; }

bool quatNear(const traj::Quat& a, const traj::Quat& b, double tol) {
    return near(a.w, b.w, tol) && near(a.x, b.x, tol) && near(a.y, b.y, tol) &&
           near(a.z, b.z, tol);
}

// Sign-agnostic comparison: q and -q are the same rotation.
bool quatEquiv(const traj::Quat& a, const traj::Quat& b, double tol) {
    return quatNear(a, b, tol) || quatNear(a, -b, tol);
}

bool unitNorm(const traj::Quat& q, double tol = 1e-12) {
    return near(q.norm(), 1.0, tol);
}

traj::Quat axisAngle(double ax, double ay, double az, double angleRad) {
    double n = std::sqrt(ax * ax + ay * ay + az * az);
    double s = std::sin(angleRad / 2.0) / n;
    return {std::cos(angleRad / 2.0), ax * s, ay * s, az * s};
}

traj::Pose pose(double t, traj::Vec3 p, traj::Quat q) {
    traj::Pose r;
    r.t = t;
    r.position = p;
    r.orientation = q;
    return r;
}

constexpr double kTol = 1e-12;

void testEndpointConsistency() {
    // Endpoints must be returned bit-exact, position and orientation alike.
    traj::Quat q0 = axisAngle(0, 0, 1, 0.3);
    traj::Quat q1 = axisAngle(1, 2, 3, -1.1);
    traj::Trajectory tr({pose(2.0, {1, 2, 3}, q0), pose(5.0, {-4, 0.5, 2}, q1)}, {});
    auto r0 = tr.query(2.0);
    auto r1 = tr.query(5.0);
    check(r0.ok && r1.ok, "endpoints: queries ok");
    check(r0.pose.position.x == 1 && r0.pose.position.y == 2 && r0.pose.position.z == 3,
          "endpoints: position t0 exact");
    check(r1.pose.position.x == -4 && r1.pose.position.y == 0.5 && r1.pose.position.z == 2,
          "endpoints: position t1 exact");
    check(quatNear(r0.pose.orientation, q0.normalized(), 0.0), "endpoints: orientation t0 exact");
    check(quatNear(r1.pose.orientation, q1.normalized(), 0.0), "endpoints: orientation t1 exact");
}

void testLinearPosition() {
    traj::Trajectory tr({pose(0.0, {0, 0, 0}, {}), pose(4.0, {8, -4, 2}, {})}, {});
    auto r = tr.query(1.0);
    check(r.ok && near(r.pose.position.x, 2.0, kTol) && near(r.pose.position.y, -1.0, kTol) &&
              near(r.pose.position.z, 0.5, kTol),
          "position: linear interpolation at u=0.25");
}

void testNear180Degrees() {
    // Rotation of 179.9 deg about z: quaternion dot product ~ 0 (sin(89.95deg)),
    // the ill-conditioned end of the SLERP domain.
    const double angle = 179.9 * M_PI / 180.0;
    traj::Quat q0;  // identity
    traj::Quat q1 = axisAngle(0, 0, 1, angle);
    traj::Trajectory tr({pose(0.0, {0, 0, 0}, q0), pose(2.0, {0, 0, 0}, q1)}, {});
    auto mid = tr.query(1.0);
    check(mid.ok, "180deg: midpoint query ok");
    check(unitNorm(mid.pose.orientation), "180deg: midpoint unit norm");
    // Midpoint must be the 89.95 deg rotation about z.
    check(quatEquiv(mid.pose.orientation, axisAngle(0, 0, 1, angle / 2.0), 1e-9),
          "180deg: midpoint is half the rotation");
    // Full sweep stays on the unit sphere and rotates monotonically about z.
    bool allUnit = true, allAxisZ = true;
    for (int i = 0; i <= 200; ++i) {
        auto r = tr.query(2.0 * i / 200.0);
        allUnit = allUnit && r.ok && unitNorm(r.pose.orientation, 1e-9);
        allAxisZ = allAxisZ && near(r.pose.orientation.x, 0.0, 1e-12) &&
                   near(r.pose.orientation.y, 0.0, 1e-12);
    }
    check(allUnit, "180deg: unit norm along full sweep");
    check(allAxisZ, "180deg: rotation axis stays z");
}

void testExact180Degrees() {
    // Exactly 180 deg: dot == 0, sin(theta) == 1, no degeneracy, but worth
    // pinning down.
    traj::Quat q1 = axisAngle(0, 0, 1, M_PI);  // (0,0,0,1)
    traj::Trajectory tr({pose(0.0, {0, 0, 0}, {}), pose(1.0, {0, 0, 0}, q1)}, {});
    auto mid = tr.query(0.5);
    check(mid.ok && unitNorm(mid.pose.orientation), "exact180: midpoint unit norm");
    check(quatEquiv(mid.pose.orientation, axisAngle(0, 0, 1, M_PI / 2.0), 1e-12),
          "exact180: midpoint is 90 deg about z");
}

void testSmallAngle() {
    // 1 microradian: far below the linear-fallback threshold.
    traj::Quat q1 = axisAngle(0, 1, 0, 1e-6);
    traj::Trajectory tr({pose(0.0, {0, 0, 0}, {}), pose(3.0, {0, 0, 0}, q1)}, {});
    auto mid = tr.query(1.5);
    check(mid.ok && unitNorm(mid.pose.orientation), "small-angle: midpoint unit norm");
    check(quatEquiv(mid.pose.orientation, axisAngle(0, 1, 0, 5e-7), 1e-12),
          "small-angle: midpoint is half the rotation");
    // Identical quaternions: degenerate interval must yield the constant.
    traj::Trajectory tr2({pose(0.0, {0, 0, 0}, q1), pose(3.0, {0, 0, 0}, q1)}, {});
    auto c = tr2.query(1.5);
    check(c.ok && quatNear(c.pose.orientation, q1.normalized(), 0.0),
          "small-angle: identical endpoints give constant");
}

void testOppositeSignQuaternions() {
    // q and -q are the same rotation; SLERP must take the shortest path and
    // both spellings must give identical results.
    traj::Quat q0 = axisAngle(1, 0, 0, 0.4);
    traj::Quat q1 = axisAngle(1, 0, 0, 0.9);
    traj::Quat q1neg = -q1;
    traj::Trajectory trPlus({pose(0.0, {0, 0, 0}, q0), pose(2.0, {0, 0, 0}, q1)}, {});
    traj::Trajectory trMinus({pose(0.0, {0, 0, 0}, q0), pose(2.0, {0, 0, 0}, q1neg)}, {});
    bool same = true, sameStrict = true, allUnit = true;
    for (int i = 0; i <= 50; ++i) {
        double t = 2.0 * i / 50.0;
        auto a = trPlus.query(t);
        auto b = trMinus.query(t);
        // At keyframes the stored pose is returned verbatim, so signs may
        // differ there; the rotation must always be the same.
        same = same && a.ok && b.ok && quatEquiv(a.pose.orientation, b.pose.orientation, 1e-12);
        if (i != 0 && i != 50)
            sameStrict = sameStrict && quatNear(a.pose.orientation, b.pose.orientation, 1e-12);
        allUnit = allUnit && unitNorm(a.pose.orientation) && unitNorm(b.pose.orientation);
    }
    check(same, "opposite-sign: q and -q give the same rotation everywhere");
    check(sameStrict, "opposite-sign: interior samples bit-identical after sign flip");
    check(allUnit, "opposite-sign: unit norm along both sweeps");
    // Shortest path: midpoint stays near the 0.65 rad rotation, not the long
    // way around through ~2*pi.
    auto mid = trMinus.query(1.0);
    check(quatEquiv(mid.pose.orientation, axisAngle(1, 0, 0, 0.65), 1e-9),
          "opposite-sign: shortest path taken");
}

void testAntipodalExact() {
    // q1 == -q0 exactly: after sign flip the endpoints coincide, result must
    // be the constant rotation (no NaN from the 0/0 SLERP form).
    traj::Quat q0 = axisAngle(0.3, -0.7, 1.0, 1.2);
    traj::Trajectory tr({pose(0.0, {0, 0, 0}, q0), pose(2.0, {0, 0, 0}, -q0)}, {});
    bool constant = true, allUnit = true;
    for (int i = 0; i <= 20; ++i) {
        auto r = tr.query(2.0 * i / 20.0);
        constant = constant && r.ok && quatEquiv(r.pose.orientation, q0, 1e-12);
        allUnit = allUnit && r.ok && unitNorm(r.pose.orientation);
    }
    check(constant, "antipodal: constant rotation");
    check(allUnit, "antipodal: unit norm");
}

void testUnitNormSweep() {
    // A generic long sweep across several segments.
    traj::Trajectory tr({pose(0.0, {0, 0, 0}, axisAngle(1, 0, 0, 0.1)),
                         pose(1.0, {1, 0, 0}, axisAngle(0, 1, 0, 2.5)),
                         pose(2.5, {1, 1, 0}, axisAngle(0, 0, 1, -2.9)),
                         pose(4.0, {1, 1, 1}, axisAngle(1, 1, 1, 3.0))},
                        {});
    bool allUnit = true;
    for (int i = 0; i <= 1000; ++i) {
        auto r = tr.query(4.0 * i / 1000.0);
        allUnit = allUnit && r.ok && unitNorm(r.pose.orientation, 1e-9);
    }
    check(allUnit, "unit norm: 1001-sample sweep across segments");
}

void testGapRejection() {
    // Interval (1s, 10s) exceeds max_gap_seconds=5: interior queries must be
    // rejected, keyframe queries must still succeed.
    traj::Options opts;
    opts.max_gap_seconds = 5.0;
    traj::Trajectory tr({pose(0.0, {0, 0, 0}, {}), pose(1.0, {1, 0, 0}, {}),
                         pose(10.0, {2, 0, 0}, {}), pose(11.0, {3, 0, 0}, {})},
                        opts);
    check(tr.query(0.5).ok, "gap: short interval before gap ok");
    auto inGap = tr.query(5.0);
    check(!inGap.ok && inGap.error_code == "gap", "gap: interior of gap rejected");
    check(tr.query(1.0).ok, "gap: keyframe at gap start ok");
    check(tr.query(10.0).ok, "gap: keyframe at gap end ok");
    check(tr.query(10.5).ok, "gap: short interval after gap ok");
    // Exactly max_gap is not a gap (policy: strictly longer).
    traj::Trajectory tr2({pose(0.0, {0, 0, 0}, {}), pose(5.0, {1, 0, 0}, {})}, opts);
    check(tr2.query(2.5).ok, "gap: interval equal to max_gap allowed");
}

void testDuplicateTimestampsRejected() {
    bool threw = false;
    try {
        traj::Trajectory tr({pose(1.0, {0, 0, 0}, {}), pose(1.0, {1, 0, 0}, {})}, {});
    } catch (const std::invalid_argument& e) {
        threw = std::string(e.what()).find("duplicate") != std::string::npos;
    }
    check(threw, "duplicates: duplicate timestamps rejected");
}

void testExtrapolationPolicies() {
    traj::Trajectory clamped({pose(0.0, {0, 0, 0}, {}), pose(1.0, {1, 2, 3}, {})}, {});
    auto before = clamped.query(-2.0);
    auto after = clamped.query(7.5);
    check(before.ok && before.pose.position.x == 0.0, "extrapolation: clamp before range");
    check(after.ok && after.pose.position.x == 1.0 && after.pose.position.y == 2.0,
          "extrapolation: clamp after range");
    check(before.pose.t == -2.0 && after.pose.t == 7.5,
          "extrapolation: clamped pose carries query time");

    traj::Options opts;
    opts.extrapolation = traj::ExtrapolationPolicy::Error;
    traj::Trajectory strict({pose(0.0, {0, 0, 0}, {}), pose(1.0, {1, 0, 0}, {})}, opts);
    auto r = strict.query(-1e-9);
    check(!r.ok && r.error_code == "out_of_range", "extrapolation: error policy before range");
    r = strict.query(1.0 + 1e-9);
    check(!r.ok && r.error_code == "out_of_range", "extrapolation: error policy after range");
    check(strict.query(0.5).ok, "extrapolation: error policy in range ok");
}

void testUnsortedInputAndSingleKeyframe() {
    traj::Trajectory tr({pose(2.0, {2, 0, 0}, {}), pose(0.0, {0, 0, 0}, {}),
                         pose(1.0, {1, 0, 0}, {})},
                        {});
    auto r = tr.query(0.5);
    check(r.ok && near(r.pose.position.x, 0.5, kTol), "sorting: unsorted keyframes handled");

    traj::Trajectory single({pose(3.0, {7, 8, 9}, axisAngle(1, 0, 0, 1.0))}, {});
    auto s = single.query(3.0);
    check(s.ok && s.pose.position.x == 7, "single keyframe: exact query ok");
    auto sClamp = single.query(100.0);
    check(sClamp.ok && sClamp.pose.position.x == 7, "single keyframe: clamp constant");
}

void testInvalidInputRejected() {
    bool zeroNorm = false, empty = false, nanPose = false;
    try {
        traj::Trajectory tr({pose(0.0, {0, 0, 0}, {0, 0, 0, 0})}, {});
    } catch (const std::invalid_argument&) {
        zeroNorm = true;
    }
    try {
        traj::Trajectory tr({}, {});
    } catch (const std::invalid_argument&) {
        empty = true;
    }
    try {
        traj::Trajectory tr({pose(0.0, {0, NAN, 0}, {})}, {});
    } catch (const std::invalid_argument&) {
        nanPose = true;
    }
    check(zeroNorm, "invalid: zero-norm quaternion rejected");
    check(empty, "invalid: empty keyframe list rejected");
    check(nanPose, "invalid: NaN position rejected");
}

}  // namespace

int main() {
    testEndpointConsistency();
    testLinearPosition();
    testNear180Degrees();
    testExact180Degrees();
    testSmallAngle();
    testOppositeSignQuaternions();
    testAntipodalExact();
    testUnitNormSweep();
    testGapRejection();
    testDuplicateTimestampsRejected();
    testExtrapolationPolicies();
    testUnsortedInputAndSingleKeyframe();
    testInvalidInputRejected();

    std::printf("%d checks, %d failures\n", g_checks, g_failures);
    return g_failures == 0 ? 0 : 1;
}
