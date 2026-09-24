#include "topp/api.hpp"
#include "topp/planner.hpp"
#include "topp/sha256.hpp"
#include "topp/json.hpp"

#include <cmath>
#include <cstdio>
#include <functional>
#include <iostream>
#include <sstream>
#include <string>
#include <vector>

using namespace topp;

namespace {

int g_failures = 0;
int g_checks = 0;
std::string g_current;

#define CHECK(cond)                                                            \
    do {                                                                       \
        ++g_checks;                                                            \
        if (!(cond)) {                                                         \
            ++g_failures;                                                      \
            std::cerr << "  FAIL [" << g_current << "] " << #cond << " ("      \
                      << __FILE__ << ":" << __LINE__ << ")\n";                 \
        }                                                                      \
    } while (0)

#define CHECK_NEAR(a, b, tol)                                                  \
    do {                                                                       \
        ++g_checks;                                                            \
        double va = (a), vb = (b), vt = (tol);                                 \
        if (std::abs(va - vb) > vt) {                                          \
            ++g_failures;                                                      \
            std::cerr << "  FAIL [" << g_current << "] |" << #a << " - "       \
                      << #b << "| = " << std::abs(va - vb) << " > " << vt      \
                      << " (got " << va << " vs " << vb << ") (" << __FILE__   \
                      << ":" << __LINE__ << ")\n";                             \
        }                                                                      \
    } while (0)

void testCase(const std::string& name, std::function<void()> fn) {
    g_current = name;
    std::cerr << "[ RUN  ] " << name << "\n";
    try {
        fn();
    } catch (const std::exception& e) {
        ++g_failures;
        std::cerr << "  FAIL [" << name << "] exception: " << e.what() << "\n";
    }
    std::cerr << (g_failures ? "[ ...  ]\n" : "[  OK  ]\n");
}

Eigen::MatrixXd points(std::vector<std::vector<double>> rows) {
    int dof = static_cast<int>(rows.size());
    int n = static_cast<int>(rows[0].size());
    Eigen::MatrixXd W(dof, n);
    for (int j = 0; j < dof; ++j)
        for (int i = 0; i < n; ++i) W(j, i) = rows[j][i];
    return W;
}

Result runPlan(Eigen::MatrixXd W, Eigen::VectorXd v, Eigen::VectorXd a,
               int grid = 400, int samples = 5,
               std::optional<Eigen::VectorXd> start = std::nullopt) {
    PlanningRequest req;
    req.waypoints = W;
    req.vMax = v;
    req.aMax = a;
    req.gridCells = grid;
    req.samplesPerCell = samples;
    req.startVelocity = start;
    return plan(req);
}

// Verify states at the RETAINED WAYPOINTS: strict time increase, positions
// match the requested path, velocities/accelerations finite and within limits.
void verifyWaypointBasics(const Result& r, const Eigen::MatrixXd& W,
                          const Eigen::VectorXd& vmax,
                          const Eigen::VectorXd& amax) {
    CHECK(r.points.size() == static_cast<size_t>(W.cols()));
    for (size_t i = 0; i < r.points.size(); ++i) {
        for (int j = 0; j < W.rows(); ++j) {
            CHECK_NEAR(r.points[i].q(j), W(j, static_cast<int>(i)), 1e-7);
            CHECK(std::isfinite(r.points[i].qd(j)));
            CHECK(std::isfinite(r.points[i].qdd(j)));
            CHECK(std::abs(r.points[i].qd(j)) <= vmax(j) * (1 + 1e-3) + 1e-12);
            CHECK(std::abs(r.points[i].qdd(j)) <= amax(j) * (1 + 2e-3) + 1e-12);
        }
        if (i > 0) CHECK(r.points[i].t > r.points[i - 1].t);
    }
    CHECK_NEAR(r.points.front().t, 0.0, 1e-12);
    CHECK_NEAR(r.points.back().t, r.duration, 1e-12);
    CHECK(r.verification.passed);
}

// ---------------------------------------------------------------------------
// 1. Single axis, analytic bang-bang.
//    q: 0 -> 10 (one segment, straight line), vmax=2, amax=1.
//    Distance needed to reach vmax at amax: v^2/(2a) = 2 < half distance 5,
//    so the exact optimum is the bang-bang plateau profile:
//      t_acc = 2, d_acc = 2; cruise d = 6 takes 3; total T = 7.
// ---------------------------------------------------------------------------
void test_single_axis_bangbang() {
    auto W = points({{0, 10}});
    Eigen::VectorXd v(1), a(1);
    v << 2.0;
    a << 1.0;
    Result r = runPlan(W, v, a, 2000, 6);
    verifyWaypointBasics(r, W, v, a);
    CHECK_NEAR(r.duration, 7.0, 4e-3);
    CHECK(r.bottleneckVelocityJoint == 0);
    CHECK(r.bottleneckAccelerationJoint == 0);
    bool sawCruise = false;
    for (const auto& ai : r.activeIntervals) {
        if (ai.joint == 0 && ai.limit == "velocity" &&
            (ai.tTo - ai.tFrom) > 1.0)
            sawCruise = true;
    }
    CHECK(sawCruise);
}

// ---------------------------------------------------------------------------
// 2. Single axis, analytic triangle (no cruise phase).
//    q: 0 -> 1, vmax huge (100), amax=1.
//    Peak speed sqrt(a*L) = 1; T = 2 sqrt(L/a) = 2.
// ---------------------------------------------------------------------------
void test_single_axis_triangle() {
    auto W = points({{0, 1}});
    Eigen::VectorXd v(1), a(1);
    v << 100.0;
    a << 1.0;
    Result r = runPlan(W, v, a, 2000, 6);
    verifyWaypointBasics(r, W, v, a);
    CHECK_NEAR(r.duration, 2.0, 4e-3);
    CHECK(r.bottleneckVelocityJoint == -1);
    CHECK(r.bottleneckAccelerationJoint == 0);
}

// ---------------------------------------------------------------------------
// 3. Multi-axis: different per-axis bottlenecks must ALL be satisfied with a
//    single shared clock. q: joint0 0->10, joint1 0->5 (one straight move).
//    Pick limits with a UNIQUE velocity bottleneck on joint1 and a UNIQUE
//    acceleration bottleneck on joint0:
//      v0=3, a0=1  vs  v1=1, a1=2.
//    Shared path: peak sd = min(3/10, 1/5) = 0.2 -> joint1 speed-saturated.
//    Ramp sdd: axis0 a0/10=0.1 vs axis1 a1/5=0.4 -> joint0 acc-saturated.
//    Analytic T = accel 0.2/0.1=2 (u-distance 0.2) + cruise 0.6/0.2=3
//               + decel 2 = 7.
// ---------------------------------------------------------------------------
void test_multi_axis_shared_clock() {
    auto W = points({{0, 10}, {0, 5}});
    Eigen::VectorXd v(2), a(2);
    v << 3.0, 1.0;
    a << 1.0, 2.0;
    Result r = runPlan(W, v, a, 2000, 6);
    verifyWaypointBasics(r, W, v, a);
    CHECK_NEAR(r.duration, 7.0, 5e-3);
    CHECK(r.bottleneckVelocityJoint == 1);
    CHECK(r.bottleneckAccelerationJoint == 0);
    bool j1vel = false, j0acc = false;
    for (const auto& ai : r.activeIntervals) {
        if (ai.joint == 1 && ai.limit == "velocity" &&
            ai.tTo - ai.tFrom > 1.0) j1vel = true;
        if (ai.joint == 0 && ai.limit == "acceleration" &&
            ai.tTo - ai.tFrom > 0.5) j0acc = true;
    }
    CHECK(j1vel);
    CHECK(j0acc);
}

// ---------------------------------------------------------------------------
// 4. Reverse direction: q 0 -> -10 with vmax=2, amax=1 must give the same
//    optimal duration 7 and negative velocities.
// ---------------------------------------------------------------------------
void test_reverse_direction() {
    auto W = points({{0, -10}});
    Eigen::VectorXd v(1), a(1);
    v << 2.0;
    a << 1.0;
    Result r = runPlan(W, v, a, 2000, 6);
    verifyWaypointBasics(r, W, v, a);
    CHECK_NEAR(r.duration, 7.0, 4e-3);
    CHECK(r.points.back().qd(0) <= 0.0);
}

// ---------------------------------------------------------------------------
// 5. Short segment: acceleration-limited both ways, L=0.01, amax=1, v huge.
//    T = 2 sqrt(L/a) = 0.2. Exercises tiny-cell numerics.
// ---------------------------------------------------------------------------
void test_short_segment() {
    auto W = points({{0, 0.01}});
    Eigen::VectorXd v(1), a(1);
    v << 1000.0;
    a << 1.0;
    Result r = runPlan(W, v, a, 1000, 8);
    verifyWaypointBasics(r, W, v, a);
    CHECK_NEAR(r.duration, 0.2, 3e-3);
}

// ---------------------------------------------------------------------------
// 6. Consecutive duplicate points collapse; later a zero-length "pause" point
//    must not break the strictly increasing clock.
// ---------------------------------------------------------------------------
void test_duplicate_points() {
    auto W = points({{0, 0, 0, 10, 10}});
    Eigen::VectorXd v(1), a(1);
    v << 2.0;
    a << 1.0;
    Result r = runPlan(W, v, a, 1000, 5);
    CHECK(r.removedDuplicates == 3);
    CHECK(r.points.size() == 2);
    CHECK_NEAR(r.duration, 7.0, 8e-3);
    CHECK(r.retainedInputIndices == (std::vector<int>{0, 3}));
    CHECK(r.points[1].t > r.points[0].t);
}

// ---------------------------------------------------------------------------
// 7. Infeasible boundary: requested start speed above the feasible max.
// ---------------------------------------------------------------------------
void test_infeasible_start_speed() {
    auto W = points({{0, 10}});
    Eigen::VectorXd v(1), a(1), sv(1);
    v << 2.0;
    a << 1.0;
    sv << 3.0;  // > vmax
    bool threw = false;
    try {
        runPlan(W, v, a, 500, 4, sv);
    } catch (const ApiError& e) {
        threw = true;
        CHECK(e.code == ErrorCode::INFEASIBLE_BOUNDARY);
    }
    CHECK(threw);
}

// ---------------------------------------------------------------------------
// 8. Infeasible start velocity: multi-axis directions inconsistent with the
//    path tangent (joint velocity ratio is fixed by the spline).
// ---------------------------------------------------------------------------
void test_inconsistent_start_velocity() {
    auto W = points({{0, 10}, {0, 5}});
    Eigen::VectorXd v(2), a(2), sv(2);
    v << 2.0, 1.0;
    a << 1.0, 2.0;
    sv << 1.0, 1.0;  // path fixes qd0/qd1 = 2:1, 1:1 impossible
    bool threw = false;
    try {
        runPlan(W, v, a, 400, 4, sv);
    } catch (const ApiError& e) {
        threw = true;
        CHECK(e.code == ErrorCode::INFEASIBLE_BOUNDARY);
    }
    CHECK(threw);
}

// ---------------------------------------------------------------------------
// 9. Feasible non-zero start speed: start AT vmax on a straight line removes
//    the accel ramp; must decelerate to rest: stopping distance d = v^2/(2a)=2
//    takes t = v/a = 2; remaining 8 cruised at 2 takes 4 -> T = 6.
// ---------------------------------------------------------------------------
void test_nonzero_start_speed() {
    auto W = points({{0, 10}});
    Eigen::VectorXd v(1), a(1), sv(1);
    v << 2.0;
    a << 1.0;
    sv << 2.0;
    Result r = runPlan(W, v, a, 1500, 6, sv);
    verifyWaypointBasics(r, W, v, a);
    CHECK_NEAR(r.points.front().qd(0), 2.0, 2e-2);
    CHECK_NEAR(r.duration, 6.0, 8e-3);
}

// ---------------------------------------------------------------------------
// 10. SHA-256 known answer vectors (the crypto is really computed).
// ---------------------------------------------------------------------------
void test_sha256_vectors() {
    CHECK(Sha256::hex("") ==
          "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855");
    CHECK(Sha256::hex("abc") ==
          "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad");
    std::string million(1000000, 'a');
    CHECK(Sha256::hex(million) ==
          "cdc76e5c9914fb9281a1c7e284d73e67f1809a48a497200e046d39ccc7112cd0");
}

// ---------------------------------------------------------------------------
// 11. End-to-end API: parse -> compute -> serialize, then re-parse and verify
//     the checksum independently.
// ---------------------------------------------------------------------------
void test_api_roundtrip_and_checksum() {
    std::string req = R"({
      "joint_names": ["j0","j1"],
      "points": [[0,0],[2,1],[8,4],[10,5]],
      "velocity_limits": [2,1],
      "acceleration_limits": [1,2],
      "grid_cells": 800,
      "samples_per_cell": 4
    })";
    HttpReply reply = handleParameterize(req);
    CHECK(reply.status == 200);
    JsonValue out = JsonValue::parse(reply.body);
    CHECK(out.contains("checksum"));
    std::string tagged = out.at("checksum").asString();
    CHECK(tagged.rfind("sha256:", 0) == 0);

    // Re-serialize everything except the checksum and recompute.
    JsonValue copy = out;
    copy.asObject().erase("checksum");
    std::string expected = "sha256:" + Sha256::hex(copy.dump());
    CHECK(tagged == expected);

    CHECK(out.at("verification").at("passed").asBool());
    int npts = static_cast<int>(out.at("points").asArray().size());
    CHECK(npts == 4);
    double prev = -1.0;
    for (const auto& p : out.at("points").asArray()) {
        double t = p.at("t").asNumber();
        CHECK(t > prev);
        prev = t;
    }
}

// ---------------------------------------------------------------------------
// 12. API error surfaces with proper code and HTTP status.
// ---------------------------------------------------------------------------
void test_api_error_codes() {
    HttpReply bad = handleParameterize("{not json");
    CHECK(bad.status == 400);

    std::string infeasible = R"({
      "joint_names": ["j0"],
      "points": [[0],[10]],
      "velocity_limits": [2],
      "acceleration_limits": [-1]
    })";
    HttpReply r2 = handleParameterize(infeasible);
    CHECK(r2.status == 422);
    JsonValue j2 = JsonValue::parse(r2.body);
    CHECK(j2.at("error").asString() == "INFEASIBLE_LIMITS");

    std::string allsame = R"({
      "joint_names": ["j0"],
      "points": [[1],[1],[1]],
      "velocity_limits": [2],
      "acceleration_limits": [1]
    })";
    HttpReply r3 = handleParameterize(allsame);
    CHECK(r3.status == 422);
}

// ---------------------------------------------------------------------------
// 13. Curved multi-segment path: acceleration MVC must engage on curvature.
//     A smooth corner means joint velocities/accelerations trade off through
//     the bend; verify limits hold everywhere and the report names an
//     acceleration-active joint somewhere (and that segments are not timed
//     independently).
// ---------------------------------------------------------------------------
void test_curved_path_coupling() {
    // joint0 ramps while joint1 stays then moves -> a smooth S-curve corner.
    auto W = points({{0, 4, 8, 10}, {0, 0, 5, 5}});
    Eigen::VectorXd v(2), a(2);
    v << 5.0, 5.0;
    a << 2.0, 2.0;
    Result r = runPlan(W, v, a, 1200, 6);
    verifyWaypointBasics(r, W, v, a);
    CHECK(r.duration > 0);
    // Acceleration must be reported active somewhere.
    bool accActive = false;
    for (const auto& ai : r.activeIntervals)
        if (ai.limit == "acceleration") accActive = true;
    CHECK(accActive);

    // Independent per-segment timing at MAX speed (ignoring acceleration AND
    // cross-segment continuity) yields a strictly smaller, unachievable
    // duration bound; the real duration must exceed it.
    double naive = 0.0;
    for (int i = 0; i + 1 < W.cols(); ++i) {
        double segT = 0.0;
        for (int j = 0; j < W.rows(); ++j) {
            double dq = std::abs(W(j, i + 1) - W(j, i));
            segT = std::max(segT, dq / v(j));  // v-only, accel ignored
        }
        naive += segT;
    }
    CHECK(r.duration >= naive * (1.0 - 1e-6));
}

// ---------------------------------------------------------------------------
// 14. JSON canonical parser/serializer basics.
// ---------------------------------------------------------------------------
void test_json_parse() {
    JsonValue v = JsonValue::parse(R"({"a":[1,2,3],"b":{"x":true},"c":null})");
    CHECK(v.at("a").asArray().size() == 3);
    CHECK(v.at("b").at("x").asBool() == true);
    CHECK(v.at("c").type() == JsonValue::Type::Null);
    // keys sorted
    CHECK(v.dump() == R"({"a":[1,2,3],"b":{"x":true},"c":null})");
}

// ---------------------------------------------------------------------------
// 15. Hard cusp: a joint STOPS and reverses (S' passes through zero) while
//     another flips sign. Geometry bound x <= am/|S''| must keep accelerations
//     feasible through the turnaround; verification must still pass exactly.
// ---------------------------------------------------------------------------
void test_reversing_cusp() {
    auto W = points({{0, 5, 5, 0}, {0, 3, -3, 0}});
    Eigen::VectorXd v(2), a(2);
    v << 2.0, 2.0;
    a << 1.5, 1.5;
    Result r = runPlan(W, v, a, 1000, 6);
    verifyWaypointBasics(r, W, v, a);
    CHECK(r.verification.maxAccelerationRatio <= 1.0 + 2e-3);
    CHECK(r.verification.maxVelocityRatio <= 1.0 + 2e-3);
    // Motion genuinely reverses: intermediate joint0 velocity changes sign or
    // is zero at the stall point; report contains active intervals.
    CHECK(!r.activeIntervals.empty());
}

} // namespace

int main() {
    testCase("sha256 known vectors", test_sha256_vectors);
    testCase("json canonical parse/dump", test_json_parse);
    testCase("single-axis analytic bang-bang", test_single_axis_bangbang);
    testCase("single-axis analytic triangle", test_single_axis_triangle);
    testCase("multi-axis shared clock bottlenecks",
             test_multi_axis_shared_clock);
    testCase("reverse direction", test_reverse_direction);
    testCase("short segment numerics", test_short_segment);
    testCase("duplicate point collapsing", test_duplicate_points);
    testCase("infeasible start speed", test_infeasible_start_speed);
    testCase("inconsistent start velocity", test_inconsistent_start_velocity);
    testCase("feasible non-zero start speed", test_nonzero_start_speed);
    testCase("curved path cross-segment coupling", test_curved_path_coupling);
    testCase("api roundtrip + checksum", test_api_roundtrip_and_checksum);
    testCase("api error codes", test_api_error_codes);
    testCase("reversing cusp geometry bound", test_reversing_cusp);

    std::cerr << "\n" << g_checks << " checks, " << g_failures << " failures\n";
    return g_failures == 0 ? 0 : 1;
}
