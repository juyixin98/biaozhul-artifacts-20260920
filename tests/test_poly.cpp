// Automated tests:
//   1. Fixed edge cases (vertex-ray crossings, points on hole edges, reversed
//      ring orientation, huge coordinates, invalid polygons).
//   2. Randomized differential testing: indexed slab locator vs the naive
//      exact ray-cast locator, including points snapped onto edges/vertices.
#include <algorithm>
#include <cstdint>
#include <cstdlib>
#include <iostream>
#include <random>
#include <sstream>
#include <string>
#include <vector>

#include "decimal.hpp"
#include "geometry.hpp"
#include "index.hpp"
#include "service.hpp"
#include "json.hpp"

static int g_fail = 0;
static int g_checks = 0;

#define CHECK(cond)                                                       \
    do {                                                                  \
        ++g_checks;                                                       \
        if (!(cond)) {                                                    \
            ++g_fail;                                                     \
            std::cerr << "FAIL " << __FILE__ << ":" << __LINE__           \
                      << " CHECK(" #cond ")\n";                            \
        }                                                                 \
    } while (0)

static Rel naivePoly(const Polygon& p, const Pt& q) {
    return locateNaivePolygon(p, q);
}
static Rel idxPoly(const PolygonIndex& idx, const Pt& q) {
    return point_index::locatePolygon(idx, q);
}

static Ring makeRing(std::vector<std::pair<long long, long long>> pts) {
    Ring r;
    for (auto [x, y] : pts) r.v.push_back(Pt{Int(x), Int(y)});
    r.v.push_back(r.v.front());
    return r;
}

static Polygon validPoly(Ring outer, std::vector<Ring> holes = {}) {
    Polygon p;
    p.outer = std::move(outer);
    p.holes = std::move(holes);
    ValidationResult vr = validateAndNormalizeRing(p.outer);
    if (!vr.ok) throw std::runtime_error("bad outer in fixture: " + vr.message);
    for (Ring& h : p.holes) {
        vr = validateAndNormalizeRing(h);
        if (!vr.ok) throw std::runtime_error("bad hole in fixture: " + vr.message);
    }
    vr = validatePolygon(p);
    if (!vr.ok) throw std::runtime_error("bad polygon in fixture: " + vr.message);
    return p;
}

// -------------------------------------------------------------------------
// Fixed cases
// -------------------------------------------------------------------------

static void testBasicBox() {
    Polygon p = validPoly(makeRing({{0, 0}, {10, 0}, {10, 10}, {0, 10}}));
    PolygonIndex idx;
    idx.build(p);
    CHECK(naivePoly(p, {5, 5}) == Rel::Inside);
    CHECK(idxPoly(idx, {5, 5}) == Rel::Inside);
    CHECK(idxPoly(idx, {-1, 5}) == Rel::Outside);
    CHECK(idxPoly(idx, {5, 10}) == Rel::Bound);   // top edge
    CHECK(idxPoly(idx, {0, 5}) == Rel::Bound);    // left edge
    CHECK(idxPoly(idx, {10, 0}) == Rel::Bound);   // corner
    CHECK(idxPoly(idx, {10, 10}) == Rel::Bound);
    CHECK(idxPoly(idx, {0, 0}) == Rel::Bound);
    CHECK(idxPoly(idx, {20, 20}) == Rel::Outside);
    CHECK(idxPoly(idx, {5, 0}) == Rel::Bound);    // bottom edge interior
}

// Horizontal ray passing through vertices at varying y levels.
static void testVertexRay() {
    // Diamond (45-degree), plus a point whose ray threads two vertices.
    Polygon p = validPoly(makeRing({{0, 2}, {2, 0}, {4, 2}, {2, 4}}));
    PolygonIndex idx;
    idx.build(p);

    // Ray at y=2 goes through left vertex (0,2) and right vertex (4,2).
    CHECK(idxPoly(idx, {2, 2}) == Rel::Inside);
    CHECK(naivePoly(p, {2, 2}) == Rel::Inside);
    CHECK(idxPoly(idx, {0, 2}) == Rel::Bound);
    CHECK(idxPoly(idx, {4, 2}) == Rel::Bound);
    CHECK(idxPoly(idx, {-1, 2}) == Rel::Outside);
    CHECK(idxPoly(idx, {5, 2}) == Rel::Outside);
    // Ray y=0 through bottom vertex only.
    CHECK(idxPoly(idx, {2, 0}) == Rel::Bound);
    CHECK(idxPoly(idx, {2, 1}) == Rel::Inside);
    CHECK(idxPoly(idx, {2, 3}) == Rel::Inside);

    // Notch polygon: square with a V-notch from the top; the horizontal ray at
    // y=5 crosses 4 vertices' heights indirectly. Explicit vertex tests.
    Polygon q = validPoly(makeRing({
        {0, 0}, {10, 0}, {10, 10}, {7, 10}, {5, 6}, {3, 10}, {0, 10}}));
    PolygonIndex iq;
    iq.build(q);
    CHECK(naivePoly(q, {5, 6}) == Rel::Bound); // notch tip
    CHECK(idxPoly(iq, {5, 6}) == Rel::Bound);
    CHECK(idxPoly(iq, {5, 8}) == Rel::Outside); // inside notch
    CHECK(naivePoly(q, {5, 8}) == Rel::Outside);
    CHECK(idxPoly(iq, {1, 8}) == Rel::Inside);
    CHECK(idxPoly(iq, {9, 8}) == Rel::Inside);
    CHECK(idxPoly(iq, {5, 5}) == Rel::Inside);
}

// Points on hole edges and inside/outside holes.
static void testHoles() {
    Ring outer = makeRing({{0, 0}, {20, 0}, {20, 20}, {0, 20}});
    Ring hole = makeRing({{5, 5}, {15, 5}, {15, 15}, {5, 15}}); // CCW input, canonicalized to CW
    Polygon p = validPoly(std::move(outer), {std::move(hole)});
    PolygonIndex idx;
    idx.build(p);
    CHECK(idxPoly(idx, {10, 10}) == Rel::Outside); // inside hole
    CHECK(naivePoly(p, {10, 10}) == Rel::Outside);
    CHECK(idxPoly(idx, {2, 2}) == Rel::Inside);
    CHECK(idxPoly(idx, {18, 18}) == Rel::Inside);
    CHECK(idxPoly(idx, {10, 5}) == Rel::Bound);    // hole bottom edge
    CHECK(naivePoly(p, {10, 5}) == Rel::Bound);
    CHECK(idxPoly(idx, {5, 10}) == Rel::Bound);    // hole left edge
    CHECK(idxPoly(idx, {15, 10}) == Rel::Bound);
    CHECK(idxPoly(idx, {5, 5}) == Rel::Bound);     // hole corner
    CHECK(idxPoly(idx, {10, 0}) == Rel::Bound);    // outer edge
}

// Reversing every ring's direction must not change answers.
static void testOrientation() {
    Ring outerCCW = makeRing({{0, 0}, {20, 0}, {20, 20}, {0, 20}});
    Ring outerCW = makeRing({{0, 0}, {0, 20}, {20, 20}, {20, 0}});
    Ring holeCCW = makeRing({{5, 5}, {5, 15}, {15, 15}, {15, 5}}); // CCW input
    Ring holeCW = makeRing({{5, 5}, {15, 5}, {15, 15}, {5, 15}});  // CW input

    Polygon p1 = validPoly(outerCCW, {holeCW});
    Polygon p2 = validPoly(outerCW, {holeCCW});
    PolygonIndex i1, i2;
    i1.build(p1);
    i2.build(p2);
    std::vector<Pt> qs = {
        {1, 1}, {10, 10}, {5, 5}, {15, 15}, {10, 5}, {3, 12},
        {17, 3}, {0, 0}, {-1, 5}, {20, 20}, {7, 7}, {2, 18}};
    for (const Pt& q : qs) {
        Rel a = idxPoly(i1, q);
        Rel b = idxPoly(i2, q);
        CHECK(a == b);
        CHECK(naivePoly(p1, q) == a);
        CHECK(naivePoly(p2, q) == b);
    }
}

// Huge coordinates: cpp_int keeps this exact; doubles would round.
static void testHugeCoordinates() {
    Int B(1);
    for (int i = 0; i < 40; ++i) B *= 10; // 10^40
    auto big = [&](long long v) { return B + v; };
    Ring r;
    r.v = {{big(0), big(0)}, {big(100), big(0)}, {big(100), big(100)},
           {big(0), big(100)}};
    r.v.push_back(r.v.front());
    Polygon p = validPoly(r);
    PolygonIndex idx;
    idx.build(p);
    // Point 1 unit inside the lower-left corner; naive double-based code
    // could not distinguish these at 10^40.
    Pt inside{big(1), big(1)};
    Pt outside{big(-1), big(1)};
    Pt onEdge{big(50), big(0)};
    CHECK(naivePoly(p, inside) == Rel::Inside);
    CHECK(idxPoly(idx, inside) == Rel::Inside);
    CHECK(idxPoly(idx, outside) == Rel::Outside);
    CHECK(idxPoly(idx, onEdge) == Rel::Bound);

    // Fractional scaling through the service: 0.1 and 0.000000001 precision.
    Service svc;
    std::string req = R"({
      "op": "locate",
      "compare_with_naive": true,
      "polygon": {
        "outer": [["0.0","0.0"],["10.0","0.0"],["10.0","10.0"],["0.0","10.0"]]
      },
      "points": [["0.1","0.1"], ["0.000000001","0.000000001"],
                 ["10.0","5.0"], ["9.999999999","9.999999999"]]
    })";
    std::string err;
    JsonPtr out = svc.handleNode(*jsonParse(req), &err);
    const JsonPtr* res = out->get("results");
    CHECK(res != nullptr);
    CHECK((*res)->arr.size() == 4);
    CHECK((*res)->arr[0]->get("location")->get()->str == "inside");
    CHECK((*res)->arr[1]->get("location")->get()->str == "inside");
    CHECK((*res)->arr[2]->get("location")->get()->str == "boundary");
    CHECK((*res)->arr[3]->get("location")->get()->str == "inside");
    for (const JsonPtr& item : (*res)->arr)
        CHECK(item->get("agree")->get()->boolean == true);
}

// Rings/polygons that must be rejected.
static void testValidationFailures() {
    auto expectRingFail = [](std::vector<std::pair<long long, long long>> pts,
                             const std::string& code) {
        Ring r;
        for (auto [x, y] : pts) r.v.push_back(Pt{Int(x), Int(y)});
        if (r.v.front() != r.v.back()) r.v.push_back(r.v.front());
        ValidationResult vr = validateAndNormalizeRing(r);
        CHECK(!vr.ok);
        CHECK(vr.errorCode == code);
    };

    expectRingFail({{0, 0}, {10, 0}, {0, 0}}, "RING_TOO_SHORT"); // 2 distinct
    // Three distinct but collinear points: closing the ring backtracks over
    // the same edges, which overlap.
    expectRingFail({{0, 0}, {5, 0}, {10, 0}}, "SELF_INTERSECTING_RING");
    // Bowtie / figure-eight: self-intersecting (also zero signed area; the
    // self-intersection must be reported first).
    expectRingFail({{0, 0}, {10, 10}, {10, 0}, {0, 10}},
                   "SELF_INTERSECTING_RING");
    // Repeated non-consecutive vertex.
    expectRingFail({{0, 0}, {10, 0}, {10, 10}, {0, 0}, {0, 10}},
                   "DUPLICATE_VERTEX");
    // Collinear zero-area (same fixture: closing edges overlap).
    expectRingFail({{0, 0}, {5, 0}, {10, 0}}, "SELF_INTERSECTING_RING");

    Polygon p;
    p.outer = makeRing({{0, 0}, {20, 0}, {20, 20}, {0, 20}});
    ValidationResult vr = validateAndNormalizeRing(p.outer);
    CHECK(vr.ok);

    // Hole outside the outer ring.
    p.holes = {makeRing({{30, 30}, {40, 30}, {40, 40}, {30, 40}})};
    for (Ring& h : p.holes) CHECK(validateAndNormalizeRing(h).ok);
    vr = validatePolygon(p);
    CHECK(!vr.ok);
    CHECK(vr.errorCode == "HOLE_OUTSIDE_OUTER");

    // Hole touching the outer edge.
    p.holes = {makeRing({{0, 5}, {10, 5}, {10, 15}, {0, 15}})};
    for (Ring& h : p.holes) CHECK(validateAndNormalizeRing(h).ok);
    vr = validatePolygon(p);
    CHECK(!vr.ok);
    CHECK(vr.errorCode == "HOLE_OUTSIDE_OUTER");

    // Two overlapping holes.
    p.holes = {makeRing({{2, 2}, {12, 2}, {12, 12}, {2, 12}}),
               makeRing({{8, 8}, {18, 8}, {18, 18}, {8, 18}})};
    for (Ring& h : p.holes) CHECK(validateAndNormalizeRing(h).ok);
    vr = validatePolygon(p);
    CHECK(!vr.ok);
    CHECK(vr.errorCode == "INTERSECTING_HOLES");

    // Nested holes.
    p.holes = {makeRing({{2, 2}, {18, 2}, {18, 18}, {2, 18}}),
               makeRing({{5, 5}, {10, 5}, {10, 10}, {5, 10}})};
    for (Ring& h : p.holes) CHECK(validateAndNormalizeRing(h).ok);
    vr = validatePolygon(p);
    CHECK(!vr.ok);
    CHECK(vr.errorCode == "NESTED_HOLES");
}

// Service JSON behaviors.
static void testService() {
    Service svc;

    // Reversed-orientation input must be accepted and canonicalized.
    std::string req = R"({
      "op": "locate",
      "compare_with_naive": true,
      "polygon": {
        "outer": [[0,0],[0,10],[10,10],[10,0]],
        "holes": [ [[2,2],[2,8],[8,8],[8,2]] ]
      },
      "points": [[1,1],[5,5],[2,5],[9,9]]
    })";
    std::string err;
    JsonPtr out = svc.handleNode(*jsonParse(req), &err);
    CHECK(out->get("status")->get()->str == "ok");
    CHECK(out->get("meta")->get()->get("outer_input_orientation")->get()->str ==
          "cw");
    const auto& results = out->get("results")->get()->arr;
    CHECK(results[0]->get("location")->get()->str == "inside");
    CHECK(results[1]->get("location")->get()->str == "outside"); // hole
    CHECK(results[2]->get("location")->get()->str == "boundary");
    CHECK(results[3]->get("location")->get()->str == "inside");
    for (const JsonPtr& i : results)
        CHECK(i->get("agree")->get()->boolean);

    // Malformed JSON => error response.
    Service svc2;
    std::string resp = svc2.handle("{not json", false);
    JsonPtr e = jsonParse(resp);
    CHECK(e->get("status")->get()->str == "error");
    CHECK(e->get("error")->get()->get("code")->get()->str == "MALFORMED_JSON");

    // Invalid polygon request reports invalid_polygon, not a crash.
    Service svc3;
    std::string bad = R"({"op":"locate","polygon":{
      "outer":[[0,0],[10,10],[10,0],[0,10]]},"points":[[1,1]]})";
    resp = svc3.handle(bad, false);
    e = jsonParse(resp);
    CHECK(e->get("status")->get()->str == "invalid_polygon");
    CHECK(e->get("error")->get()->get("code")->get()->str ==
          "SELF_INTERSECTING_RING");

    // prepare op then validate.
    Service svc4;
    resp = svc4.handle(R"({"op":"prepare","polygon":{
      "outer":[[0,0],[10,0],[10,10],[0,10]]}})", false);
    e = jsonParse(resp);
    CHECK(e->get("prepared")->get()->boolean == true);
}

// -------------------------------------------------------------------------
// Randomized differential testing
// -------------------------------------------------------------------------

// x-monotone simple polygon (guaranteed simple) inside a bounding box.
static Polygon randomMonotonePolygon(std::mt19937_64& rng, long long W,
                                     long long H, int holes) {
    std::uniform_int_distribution<long long> xd(2, W - 2);
    int n = 8 + static_cast<int>(rng() % 14);
    std::vector<long long> xs;
    for (int i = 0; i < n; ++i) xs.push_back(xd(rng));
    std::sort(xs.begin(), xs.end());
    xs.erase(std::unique(xs.begin(), xs.end()), xs.end());
    n = static_cast<int>(xs.size());

    std::vector<std::pair<long long, long long>> lower, upper;
    std::uniform_int_distribution<long long> lowD(2, H / 2 - 1);
    std::uniform_int_distribution<long long> upD(H / 2 + 1, H - 2);
    for (int i = 0; i < n; ++i) {
        lower.push_back({xs[i], lowD(rng)});
        upper.push_back({xs[i], upD(rng)});
    }
    std::vector<std::pair<long long, long long>> ring;
    ring = lower; // left -> right along lower chain
    for (int i = n - 1; i >= 0; --i) ring.push_back(upper[i]);
    Polygon p;
    p.outer = makeRing(ring);
    ValidationResult vr = validateAndNormalizeRing(p.outer);
    if (!vr.ok) throw std::runtime_error("random outer invalid: " + vr.message);

    // Axis-aligned rectangular holes placed in the lower or upper band;
    // retry until they fit strictly inside and are disjoint.
    for (int h = 0; h < holes; ++h) {
        for (int attempt = 0; attempt < 200; ++attempt) {
            std::uniform_int_distribution<long long> pd(2, W - 5);
            long long x0 = pd(rng), y0 = pd(rng);
            std::uniform_int_distribution<long long> sd(2, 5);
            long long w = sd(rng), hgt = sd(rng);
            long long x1 = x0 + w, y1 = y0 + hgt;
            if (x1 >= W - 1 || y1 >= H - 1) continue;
            Ring hr = makeRing(
                {{x0, y0}, {x1, y0}, {x1, y1}, {x0, y1}});
            ValidationResult hrvr = validateAndNormalizeRing(hr);
            if (!hrvr.ok) continue;
            Polygon candidate = p;
            candidate.holes.push_back(hr);
            if (validatePolygon(candidate).ok) {
                p.holes.push_back(hr);
                break;
            }
        }
    }
    vr = validatePolygon(p);
    if (!vr.ok) throw std::runtime_error("random polygon invalid: " + vr.message);
    return p;
}

static void differentialTest(unsigned seed, int trials) {
    std::mt19937_64 rng(seed);
    const long long W = 60, H = 60;
    std::uniform_int_distribution<long long> coord(-5, W + 5);
    int mismatch = 0;

    auto scaleRing = [](const Ring& r, long long f) {
        Ring s;
        s.hole = r.hole;
        for (const Pt& q : r.v) s.v.push_back(Pt{q.x * f, q.y * f});
        return s;
    };

    for (int t = 0; t < trials; ++t) {
        int holeCount = static_cast<int>(rng() % 3);
        Polygon p0 = randomMonotonePolygon(rng, W, H, holeCount);

        // *2-scaled copy lets half-integer query points (2x+1, 2y+1) land in
        // slab interiors rather than on vertex levels.
        Polygon p2;
        p2.outer = scaleRing(p0.outer, 2);
        for (const Ring& h : p0.holes) p2.holes.push_back(scaleRing(h, 2));
        if (!validatePolygon(p2).ok)
            throw std::runtime_error("scaled polygon invalid");

        for (int variant = 0; variant < 2; ++variant) {
            const Polygon& p = (variant == 0) ? p0 : p2;
            const long long f = (variant == 0) ? 1 : 2;
            PolygonIndex idx;
            idx.build(p);

            std::vector<Pt> specials;
            auto addRingSpecials = [&](const Ring& r) {
                for (size_t i = 0; i + 1 < r.v.size(); ++i) {
                    specials.push_back(r.v[i]);
                    if (variant == 0)
                        specials.push_back(Pt{(r.v[i].x + r.v[i + 1].x) / 2,
                                              (r.v[i].y + r.v[i + 1].y) / 2});
                }
            };
            addRingSpecials(p.outer);
            for (const Ring& h : p.holes) addRingSpecials(h);

            auto checkQ = [&](const Pt& q) {
                ++g_checks;
                Rel a = naivePoly(p, q);
                Rel b = idxPoly(idx, q);
                if (a != b) {
                    ++mismatch;
                    ++g_fail;
                    std::cerr << "MISMATCH seed=" << seed << " trial=" << t
                              << " variant=" << variant << " q=(" << q.x << ","
                              << q.y << ") naive=" << relName(a)
                              << " index=" << relName(b) << "\n";
                }
            };

            for (const Pt& q : specials) checkQ(q);
            // Integer grid (hits vertex levels).
            for (long long x = 0; x <= W; x += 3)
                for (long long y = 0; y <= H; y += 3)
                    checkQ(Pt{Int(x * f), Int(y * f)});
            if (variant == 1) {
                // Half-integer grid on the *2 scale (slab interiors).
                for (long long x = 0; x <= W; x += 4)
                    for (long long y = 0; y <= H; y += 4)
                        checkQ(Pt{2 * x + 1, 2 * y + 1});
            }
            for (int k = 0; k < 40; ++k)
                checkQ(Pt{Int(coord(rng) * f), Int(coord(rng) * f)});
        }
    }
    if (mismatch == 0)
        std::cout << "  differential: " << trials << " polygons x2 variants, no mismatches\n";
}


// Differential test with large common scale and fractional input via service.
static void fractionalServiceTest() {
    Service svc;
    std::string req = R"({
      "op": "locate",
      "compare_with_naive": true,
      "polygon": {"outer": [
        ["0.00","0.00"],["2.50","0.00"],["2.50","2.50"],
        ["1.25","3.00"],["0.00","2.50"]]},
      "points": []
    })";
    // Build many points on a fine grid.
    JsonPtr root = jsonParse(req);
    auto pts = JsonValue::makeArray();
    for (int ix = -1; ix <= 30; ++ix)
        for (int iy = -1; iy <= 32; ++iy) {
            char buf[64];
            std::snprintf(buf, sizeof(buf), "%.2f", ix / 10.0);
            auto p = JsonValue::makeArray();
            p->arr.push_back(JsonValue::makeNumber(buf));
            std::snprintf(buf, sizeof(buf), "%.2f", iy / 10.0);
            p->arr.push_back(JsonValue::makeNumber(buf));
            pts->arr.push_back(p);
        }
    root->set("points", pts);
    std::string err;
    JsonPtr out = svc.handleNode(*root, &err);
    CHECK(out->get("status")->get()->str == "ok");
    int bad = 0;
    for (const JsonPtr& item : out->get("results")->get()->arr)
        if (!item->get("agree")->get()->boolean) ++bad;
    CHECK(bad == 0);
}

int main() {
    try {
    testBasicBox();
    testVertexRay();
    testHoles();
    testOrientation();
    testHugeCoordinates();
    testValidationFailures();
    testService();
    fractionalServiceTest();

    std::cout << "Running randomized differential tests...\n";
    differentialTest(/*seed=*/12345, /*trials=*/300);
    differentialTest(/*seed=*/999, /*trials=*/100);

    } catch (const std::exception& ex) {
        std::cerr << "UNCAUGHT: " << ex.what() << "\n";
        return 2;
    }
    std::cout << (g_fail == 0 ? "ALL TESTS PASSED" : "TESTS FAILED")
              << " (" << g_checks << " checks, " << g_fail << " failures)\n";
    return g_fail == 0 ? 0 : 1;
}
