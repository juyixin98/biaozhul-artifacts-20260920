// Unit tests for exact predicates, validity checks and the two engines.
// Build: make unit_tests ; ./unit_tests
#include <algorithm>
#include <cmath>
#include <cstdint>
#include <cstdio>
#include <random>
#include <string>
#include <vector>

#include "geometry.hpp"
#include "locator.hpp"

using geo::Location;
using geo::Point;
using geo::i64;

static int g_failures = 0;
static int g_checks = 0;

#define CHECK(cond)                                                        \
    do {                                                                   \
        ++g_checks;                                                        \
        if (!(cond)) {                                                     \
            ++g_failures;                                                  \
            std::printf("FAIL %s:%d  %s\n", __FILE__, __LINE__, #cond);    \
            std::printf("%d checks, %d failures\n", g_checks, g_failures); \
            std::exit(1);                                                  \
        }                                                                  \
    } while (0)

#define CHECK_EQ(a, b)                                                     \
    do {                                                                   \
        ++g_checks;                                                        \
        auto _va = (a);                                                    \
        auto _vb = (b);                                                    \
        if (!(_va == _vb)) {                                               \
            ++g_failures;                                                  \
            std::printf("FAIL %s:%d  %s != %s\n", __FILE__, __LINE__,      \
                        #a, #b);                                           \
        }                                                                  \
    } while (0)

namespace {

std::vector<Point> mkRect(i64 x0, i64 y0, i64 x1, i64 y1) {
    // CCW
    return {{x0, y0}, {x1, y0}, {x1, y1}, {x0, y1}};
}

void reverseRing(std::vector<Point>& r) {
    std::reverse(r.begin() + 1, r.end());
}

void testOrient() {
    Point a{0, 0}, b{10, 0};
    CHECK(geo::orient(a, b, Point{5, 1}) > 0);
    CHECK(geo::orient(a, b, Point{5, -1}) < 0);
    CHECK(geo::orient(a, b, Point{7, 0}) == 0);
    // Large coordinates: products near 6.4e37 fit in signed 128 (< 1.7e38).
    Point A{4'000'000'000'000'000'000LL, 0};
    Point B{-4'000'000'000'000'000'000LL, 0};
    Point P{0, 1};
    CHECK(geo::orient(A, B, P) < 0);
    CHECK(geo::orient(A, B, Point{123, 0}) == 0);
}

void testOnSegment() {
    Point a{0, 0}, b{10, 10};
    CHECK(geo::pointOnSegment(Point{5, 5}, a, b));
    CHECK(geo::pointOnSegment(Point{0, 0}, a, b));
    CHECK(geo::pointOnSegment(Point{10, 10}, a, b));
    CHECK(!geo::pointOnSegment(Point{5, 6}, a, b));
    CHECK(!geo::pointOnSegment(Point{11, 11}, a, b));
    Point h{2, 0}, k{8, 0};
    CHECK(geo::pointOnSegment(Point{5, 0}, h, k)); // horizontal edge interior
}

// Square (0,0)-(10,10); horizontal ray-to-+x through vertex cases.
void testRayThroughVertex() {
    auto sq = mkRect(0, 0, 10, 10);
    // Same y as the bottom edge, to its left: outside (edge excluded).
    CHECK_EQ(geo::pointInRing(Point{-5, 0}, sq), Location::Outside);
    // Same y as the top edge, to its left: outside (edge included but the
    // intersections of the two vertical edges cancel to the right).
    CHECK_EQ(geo::pointInRing(Point{-5, 10}, sq), Location::Outside);
    // Vertices and edge interiors are boundary.
    CHECK_EQ(geo::pointInRing(Point{0, 0}, sq), Location::Boundary);
    CHECK_EQ(geo::pointInRing(Point{10, 10}, sq), Location::Boundary);
    CHECK_EQ(geo::pointInRing(Point{5, 0}, sq), Location::Boundary);
    CHECK_EQ(geo::pointInRing(Point{5, 10}, sq), Location::Boundary);
    CHECK_EQ(geo::pointInRing(Point{5, 5}, sq), Location::Inside);

    // Diamond whose left/right vertices lie exactly on y=0: the +x ray from
    // the left first meets the ring at a single vertex.
    std::vector<Point> diamond = {{0, -10}, {10, 0}, {0, 10}, {-10, 0}};
    CHECK_EQ(geo::pointInRing(Point{-20, 0}, diamond), Location::Outside);
    CHECK_EQ(geo::pointInRing(Point{-10, 0}, diamond), Location::Boundary);
    CHECK_EQ(geo::pointInRing(Point{10, 0}, diamond), Location::Boundary);
    CHECK_EQ(geo::pointInRing(Point{0, 0}, diamond), Location::Inside);
    // y = 10 through the top vertex only; point left of it is outside.
    CHECK_EQ(geo::pointInRing(Point{-5, 10}, diamond), Location::Outside);
    CHECK_EQ(geo::pointInRing(Point{0, 10}, diamond), Location::Boundary);
}

void testOrientationIndependent() {
    auto ccw = mkRect(0, 0, 10, 10);
    auto cw = ccw;
    reverseRing(cw);
    CHECK(geo::ringAreaSign(ccw) > 0);
    CHECK(geo::ringAreaSign(cw) < 0);
    for (auto p : {Point{5, 5}, Point{-1, 5}, Point{0, 5}, Point{10, 3}})
        CHECK_EQ(geo::pointInRing(p, ccw), geo::pointInRing(p, cw));
}

void testValidation() {
    geo::RingError e;
    CHECK(geo::validateRing(mkRect(0, 0, 10, 10), e));
    CHECK(!geo::validateRing({{0, 0}, {1, 1}}, e));                 // too few
    CHECK(!geo::validateRing({{0, 0}, {5, 0}, {5, 0}, {0, 10}}, e)); // dup
    CHECK(!geo::validateRing({{0, 0}, {5, 0}, {10, 0}}, e));        // collinear
    // Bowtie: proper self-intersection (hourglass).
    std::vector<Point> bow = {{0, 0}, {10, 10}, {10, 0}, {0, 10}};
    CHECK(!geo::validateRing(bow, e));
    // Collinear spike: path runs out along a line and back through it.
    std::vector<Point> spike = {{0, 0}, {10, 0}, {6, 0}, {10, 10},
                                {0, 10}};
    CHECK(!geo::validateRing(spike, e));
}

void testPolygonBuild() {
    locator::Polygon poly;
    locator::BuildError be;
    CHECK(locator::buildPolygon(mkRect(0, 0, 20, 20), {mkRect(5, 5, 10, 10)},
                                poly, be));
    CHECK(!locator::buildPolygon(mkRect(0, 0, 20, 20),
                                 {mkRect(30, 30, 40, 40)}, poly, be));
    CHECK(be.kind == locator::BuildErrorKind::HoleOutsideOuter);
    CHECK(!locator::buildPolygon(mkRect(0, 0, 40, 40),
                                 {mkRect(5, 5, 20, 20),
                                  mkRect(15, 15, 30, 30)}, poly, be));
    CHECK(be.kind == locator::BuildErrorKind::HolesIntersectOrNested);
    CHECK(!locator::buildPolygon(mkRect(0, 0, 40, 40),
                                 {mkRect(5, 5, 30, 30),
                                  mkRect(10, 10, 20, 20)}, poly, be));
    CHECK(be.kind == locator::BuildErrorKind::HolesIntersectOrNested);
    CHECK(!locator::buildPolygon(mkRect(0, 0, 20, 20),
                                 {mkRect(0, 5, 10, 15)}, poly, be));
    CHECK(be.kind == locator::BuildErrorKind::HoleOutsideOuter);
}

void testPolygonWithHole() {
    locator::Polygon poly;
    locator::BuildError be;
    CHECK(locator::buildPolygon(mkRect(0, 0, 20, 20), {mkRect(5, 5, 10, 10)},
                                poly, be));
    // Hole edge: boundary.
    CHECK_EQ(locator::locateNaive(poly, Point{7, 5}), Location::Boundary);
    CHECK_EQ(locator::locateNaive(poly, Point{10, 7}), Location::Boundary);
    // Hole interior: outside of polygon.
    CHECK_EQ(locator::locateNaive(poly, Point{7, 7}), Location::Outside);
    // Ring material: inside.
    CHECK_EQ(locator::locateNaive(poly, Point{2, 2}), Location::Inside);
    CHECK_EQ(locator::locateNaive(poly, Point{15, 15}), Location::Inside);
    CHECK_EQ(locator::locateNaive(poly, Point{-1, 8}), Location::Outside);
    CHECK_EQ(locator::locateNaive(poly, Point{0, 8}), Location::Boundary);
}

// Radius-monotone star ring; rejection-retried until simple.
std::vector<Point> starRing(std::mt19937_64& rng, int n, i64 cx, i64 cy,
                            i64 scale) {
    std::uniform_int_distribution<i64> rad(scale / 2, scale);
    while (true) {
        std::vector<Point> ring;
        for (int i = 0; i < n; ++i) {
            double ang = 2.0 * M_PI * i / n;
            i64 r = rad(rng);
            ring.push_back(
                {cx + static_cast<i64>(r * std::cos(ang)),
                 cy + static_cast<i64>(r * std::sin(ang))});
        }
        geo::RingError e;
        if (geo::validateRing(ring, e)) return ring;
    }
}

// Small star ring whose every vertex is strictly inside `outer`.
std::vector<Point> containedHole(std::mt19937_64& rng, int n,
                                 const std::vector<Point>& outer,
                                 i64 cx, i64 cy, i64 scale) {
    while (true) {
        auto ring = starRing(rng, n, cx, cy, scale);
        bool allIn = true;
        for (const Point& v : ring)
            if (geo::pointInRing(v, outer) != Location::Inside) {
                allIn = false;
                break;
            }
        if (allIn) return ring;
    }
}

void testEnginesAgree() {
    std::mt19937_64 rng(12345);
    auto outer = starRing(rng, 24, 0, 0, 1000);
    auto h1 = containedHole(rng, 12, outer, -300, 250, 80);
    auto h2 = containedHole(rng, 10, outer, 350, -200, 70);
    locator::Polygon poly;
    locator::BuildError be;
    CHECK(locator::buildPolygon(outer, {h1, h2}, poly, be));

    locator::GridIndex grid;
    grid.build(poly);
    CHECK(grid.bandCount() >= 1);

    std::uniform_int_distribution<i64> coord(-1300, 1300);
    int mismatches = 0;
    for (int t = 0; t < 20000; ++t) {
        Point p{coord(rng), coord(rng)};
        if (locator::locateNaive(poly, p) != grid.query(p)) ++mismatches;
    }
    // Force queries with y exactly equal to vertex y-values (ray-through-
    // vertex degeneracy), both engines must still agree.
    std::vector<i64> ys;
    for (const auto& v : poly.outer) ys.push_back(v.y);
    for (const auto& h : poly.holes)
        for (const auto& v : h) ys.push_back(v.y);
    std::uniform_int_distribution<i64> xs(-1300, 1300);
    for (i64 y : ys)
        for (int k = 0; k < 5; ++k) {
            Point p{xs(rng), y};
            if (locator::locateNaive(poly, p) != grid.query(p)) ++mismatches;
        }
    CHECK_EQ(mismatches, 0);
}

void testLargeCoordinates() {
    const i64 B = 4'000'000'000'000'000'000LL;
    locator::Polygon poly;
    locator::BuildError be;
    CHECK(locator::buildPolygon(
        mkRect(-B, -B, B, B),
        {mkRect(-B / 4, -B / 4, B / 4, B / 4)}, poly, be));
    locator::GridIndex grid;
    grid.build(poly);
    struct Case { Point p; Location want; };
    std::vector<Case> cases = {
        {{0, 0}, Location::Outside},
        {{B / 2, B / 2}, Location::Inside},
        {{B, 0}, Location::Boundary},
        {{-B / 4, 0}, Location::Boundary},
        {{B + 1, 0}, Location::Outside},
        {{0, B + 1}, Location::Outside},
        {{B - 1, 1}, Location::Inside},
    };
    for (auto& c : cases) {
        CHECK_EQ(locator::locateNaive(poly, c.p), c.want);
        CHECK_EQ(grid.query(c.p), c.want);
    }
}

void testThinSliver() {
    const i64 C = 1'000'000'000'000'000'000LL;
    std::vector<Point> tri = {{C, C}, {C + 2, C}, {C + 1, C + 1}};
    geo::RingError e;
    CHECK(geo::validateRing(tri, e));
    locator::Polygon poly;
    locator::BuildError be;
    CHECK(locator::buildPolygon(tri, {}, poly, be));
    CHECK_EQ(locator::locateNaive(poly, Point{C + 1, C}), Location::Boundary);
}

} // namespace

int main() {
    testOrient();
    testOnSegment();
    testRayThroughVertex();
    testOrientationIndependent();
    testValidation();
    testPolygonBuild();
    testPolygonWithHole();
    testEnginesAgree();
    testLargeCoordinates();
    testThinSliver();

    std::printf("%d checks, %d failures\n", g_checks, g_failures);
    return g_failures ? 1 : 0;
}
