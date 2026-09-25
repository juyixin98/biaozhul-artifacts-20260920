// test_main.cpp — self-contained test harness (no external framework).
//
// Covers the acceptance cases: backtracking (回折), self-intersection,
// duplicate points, zero tolerance, plus degenerate inputs, the per-point
// directed error bound, and determinism of the output.
#include <cmath>
#include <cstdio>
#include <string>
#include <vector>

#include "../src/service.hpp"

namespace {

int g_failures = 0;
int g_checks = 0;

void check(bool ok, const std::string& name) {
    ++g_checks;
    if (!ok) {
        ++g_failures;
        std::printf("FAIL: %s\n", name.c_str());
    }
}

bool nearly(double a, double b, double eps = 1e-9) { return std::fabs(a - b) <= eps; }

// The directed bound every test relies on: each original point must lie
// within tolerance (plus tiny fp slack) of the simplified polyline.
bool directed_bound_holds(const std::vector<simp::Point>& pts, double tol,
                          const std::vector<std::size_t>& kept) {
    std::vector<simp::Point> line;
    for (std::size_t idx : kept) line.push_back(pts[idx]);
    const double slack = 1e-9 * (tol > 1.0 ? tol : 1.0);
    for (const auto& p : pts) {
        if (simp::dist_point_polyline(p, line) > tol + slack) return false;
    }
    return true;
}

bool endpoints_kept(const std::vector<simp::Point>& pts,
                    const std::vector<std::size_t>& kept) {
    if (pts.empty() || kept.empty()) return false;
    return kept.front() == 0 && kept.back() == pts.size() - 1;
}

void test_straight_line() {
    const std::vector<simp::Point> pts = {{0, 0}, {1, 0}, {2, 0}, {3, 0}, {4, 0}};
    const auto kept = simp::douglas_peucker(pts, 0.5);
    check(kept.size() == 2 && kept[0] == 0 && kept[1] == 4,
          "straight line collapses to endpoints");
    check(endpoints_kept(pts, kept), "straight line: endpoints kept");
    check(directed_bound_holds(pts, 0.5, kept), "straight line: bound holds");
}

void test_backtracking() {
    // Goes out to x=10 and folds all the way back over itself (回折).
    const std::vector<simp::Point> pts = {
        {0, 0}, {2, 0}, {4, 0}, {6, 0}, {8, 0}, {10, 0},
        {8, 0}, {6, 0}, {4, 0}, {2, 0}, {0, 0},
    };
    const auto kept = simp::douglas_peucker(pts, 0.1);
    check(endpoints_kept(pts, kept), "backtracking: endpoints kept");
    check(kept.size() >= 2, "backtracking: at least the fold tip survives");
    // The fold tip (10,0) at index 5 must be kept: it is 10 away from the
    // chord between the (coincident) endpoints.
    bool tip_kept = false;
    for (std::size_t idx : kept) tip_kept = tip_kept || idx == 5;
    check(tip_kept, "backtracking: fold tip kept");
    check(directed_bound_holds(pts, 0.1, kept), "backtracking: bound holds");
}

void test_self_intersection() {
    // A figure-eight style chain that crosses itself.
    const std::vector<simp::Point> pts = {
        {0, 0}, {2, 2}, {4, 0}, {2, -2}, {0, 0}, {2, 2}, {4, 0},
    };
    const auto kept = simp::douglas_peucker(pts, 0.3);
    check(endpoints_kept(pts, kept), "self-intersection: endpoints kept");
    check(directed_bound_holds(pts, 0.3, kept), "self-intersection: bound holds");
    const auto kept2 = simp::douglas_peucker(pts, 5.0);
    check(endpoints_kept(pts, kept2), "self-intersection big tol: endpoints kept");
    check(directed_bound_holds(pts, 5.0, kept2), "self-intersection big tol: bound holds");
}

void test_duplicate_points() {
    // Consecutive duplicates plus a run of identical points.
    const std::vector<simp::Point> pts = {
        {0, 0}, {0, 0}, {1, 1}, {1, 1}, {1, 1}, {2, 0}, {2, 0},
    };
    const auto kept = simp::douglas_peucker(pts, 0.5);
    check(endpoints_kept(pts, kept), "duplicates: endpoints kept");
    check(directed_bound_holds(pts, 0.5, kept), "duplicates: bound holds");

    // All points identical: everything collapses onto the endpoints.
    const std::vector<simp::Point> same = {{3, 3}, {3, 3}, {3, 3}, {3, 3}};
    const auto kept_same = simp::douglas_peucker(same, 0.0);
    check(kept_same.size() == 2 && kept_same[0] == 0 && kept_same[1] == 3,
          "all-identical points: only endpoints kept");
    check(directed_bound_holds(same, 0.0, kept_same),
          "all-identical points: bound holds with zero-length chord");
}

void test_zero_tolerance() {
    // tol = 0 keeps every vertex that is not exactly on a chord.
    const std::vector<simp::Point> pts = {
        {0, 0}, {1, 1}, {2, 0}, {3, 1}, {4, 0},
    };
    const auto kept = simp::douglas_peucker(pts, 0.0);
    check(kept.size() == pts.size(), "zero tolerance: all off-chord vertices kept");
    check(directed_bound_holds(pts, 0.0, kept), "zero tolerance: bound holds");

    // Exactly collinear intermediate vertices are dropped even at tol = 0
    // (documented, deterministic behaviour).
    const std::vector<simp::Point> col = {{0, 0}, {1, 0}, {2, 0}};
    const auto kept_col = simp::douglas_peucker(col, 0.0);
    check(kept_col.size() == 2, "zero tolerance: exactly collinear vertex dropped");
}

void test_closed_ring() {
    // First == last vertex: the outer chord is zero-length; distance must
    // fall back to point distance, not NaN.
    const std::vector<simp::Point> ring = {
        {0, 0}, {1, 0}, {1, 1}, {0, 1}, {0, 0},
    };
    const auto kept = simp::douglas_peucker(ring, 0.1);
    check(endpoints_kept(ring, kept), "closed ring: endpoints kept");
    check(kept.size() >= 3, "closed ring: corners survive degenerate chord");
    check(directed_bound_holds(ring, 0.1, kept), "closed ring: bound holds");
}

void test_tiny_inputs() {
    const std::vector<simp::Point> one = {{7, 8}};
    const auto k1 = simp::douglas_peucker(one, 1.0);
    check(k1.size() == 1 && k1[0] == 0, "single point kept");

    const std::vector<simp::Point> two = {{0, 0}, {5, 5}};
    const auto k2 = simp::douglas_peucker(two, 0.0);
    check(k2.size() == 2, "two points both kept");

    const std::vector<simp::Point> empty;
    const auto k0 = simp::douglas_peucker(empty, 1.0);
    check(k0.empty(), "empty input -> empty output (library level)");
}

void test_determinism() {
    std::vector<simp::Point> pts;
    for (int i = 0; i < 200; ++i) {
        // Deterministic wiggle with backtracking and repeats.
        pts.push_back({static_cast<double>(i % 37) * 0.5,
                       static_cast<double>((i * i) % 23) * 0.25});
    }
    const auto a = simp::douglas_peucker(pts, 0.4);
    const auto b = simp::douglas_peucker(pts, 0.4);
    check(a == b, "determinism: identical indices on repeated run");
    check(directed_bound_holds(pts, 0.4, a), "determinism case: bound holds");

    // Full pipeline twice -> byte-identical JSON.
    simp::Request req;
    req.tolerance = 0.4;
    req.points = pts;
    const std::string j1 = simp::json_serialize(simp::run_request(req));
    const std::string j2 = simp::json_serialize(simp::run_request(req));
    check(j1 == j2, "determinism: byte-identical JSON response");
}

void test_report_values() {
    // Distance from (1,1) to segment (0,0)-(2,0) is exactly 1.
    simp::Request req;
    req.tolerance = 2.0;
    req.points = {{0, 0}, {1, 1}, {2, 0}};
    const simp::Json out = simp::run_request(req);
    const simp::Json* v = out.find("validation");
    check(v != nullptr, "report: validation object present");
    if (v) {
        const simp::Json* maxd = v->find("max_distance");
        check(maxd && nearly(maxd->num, 1.0), "report: max_distance == 1");
        const simp::Json* sat = v->find("bound_satisfied");
        check(sat && sat->type == simp::Json::Type::Bool && sat->boolean,
              "report: bound_satisfied true");
        const simp::Json* per = v->find("per_point_distance");
        check(per && per->arr.size() == 3 && nearly(per->arr[1].num, 1.0),
              "report: per-point distance of apex == 1");
    }
    const simp::Json* kept = out.find("kept_indices");
    check(kept && kept->arr.size() == 2, "report: middle point dropped at tol 2");
}

void test_request_validation() {
    auto expect_error = [](const std::string& body, const std::string& name) {
        bool threw = false;
        try {
            simp::parse_request(simp::parse_json(body));
        } catch (const simp::JsonError&) {
            threw = true;
        }
        check(threw, name);
    };
    expect_error("{}", "validation: missing fields rejected");
    expect_error("{\"tolerance\": -1, \"points\": [[0,0]]}",
                 "validation: negative tolerance rejected");
    expect_error("{\"tolerance\": 1, \"points\": []}",
                 "validation: empty points rejected");
    expect_error("{\"tolerance\": 1, \"points\": [[0]]}",
                 "validation: malformed point rejected");
    expect_error("{\"tolerance\": 1, \"points\": [[0,0], null]}",
                 "validation: null point rejected");
    expect_error("[1,2,3]", "validation: non-object request rejected");

    bool threw = false;
    try {
        simp::parse_json("{\"tolerance\": 1,");
    } catch (const simp::JsonError&) {
        threw = true;
    }
    check(threw, "validation: truncated JSON rejected");
}

void test_json_roundtrip() {
    const simp::Json j = simp::parse_json(
        "{\"a\": [1, -2.5, 1e3], \"b\": \"x\\n\\u0041\", \"c\": true, \"d\": null}");
    check(j.type == simp::Json::Type::Obj, "json: object parsed");
    const simp::Json* a = j.find("a");
    check(a && a->arr.size() == 3 && nearly(a->arr[2].num, 1000.0),
          "json: array numbers parsed");
    const simp::Json* b = j.find("b");
    check(b && b->str == "x\nA", "json: string escapes decoded");
    const std::string ser = simp::json_serialize(j);
    const simp::Json j2 = simp::parse_json(ser);
    check(j2.find("b") && j2.find("b")->str == "x\nA",
          "json: serialize -> parse round-trip");
}

} // namespace

int main() {
    test_straight_line();
    test_backtracking();
    test_self_intersection();
    test_duplicate_points();
    test_zero_tolerance();
    test_closed_ring();
    test_tiny_inputs();
    test_determinism();
    test_report_values();
    test_request_validation();
    test_json_roundtrip();

    std::printf("%d checks, %d failures\n", g_checks, g_failures);
    return g_failures == 0 ? 0 : 1;
}
