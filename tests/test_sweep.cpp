//
// test_sweep.cpp — unit tests for the sweep-line engine.
//
// The reference implementation is the explicit small-grid cell enumeration
// described in the README: every cell (x,y) with integer corners is either in
// or out of the half-open union. Area is the number of in-cells; perimeter is
// the number of unit edges separating an in-cell from an out-cell (including
// the outer border). This is independent code from the sweep line, so it
// doubles as a semantic cross-check for nesting / adjacency / zero-area /
// same-x event groups.
//
#include "framework.hpp"

#include <algorithm>
#include <cstdint>
#include <random>
#include <utility>
#include <vector>

#include "../src/sweep.hpp"

using ru::i64;
using ru::Metrics;
using ru::Rect;

namespace {

struct GridRef {
    long long area = 0;
    long long perimeter = 0;
};

// rects coordinates must lie in the small shifted range; minX/minY offset
// moves negative coordinates into [0, W) x [0, H) cell-index space.
GridRef gridReference(const std::vector<Rect>& rects) {
    i64 minX = 0, minY = 0, maxX = 0, maxY = 0;
    bool first = true;
    for (const Rect& r : rects) {
        if (first) {
            minX = r.x1; minY = r.y1; maxX = r.x2; maxY = r.y2; first = false;
        } else {
            minX = std::min(minX, r.x1); minY = std::min(minY, r.y1);
            maxX = std::max(maxX, r.x2); maxY = std::max(maxY, r.y2);
        }
    }
    GridRef ref;
    if (first) return ref;

    int W = static_cast<int>(maxX - minX);
    int H = static_cast<int>(maxY - minY);
    if (W <= 0 || H <= 0) return ref;  // all-degenerate input
    std::vector<std::vector<char>> in(H, std::vector<char>(W, 0));

    auto cellIn = [&](int cx, int cy) -> bool {
        i64 x = minX + cx, y = minY + cy;
        for (const Rect& r : rects) {
            if (x >= r.x1 && x < r.x2 && y >= r.y1 && y < r.y2) return true;
        }
        return false;
    };

    for (int cy = 0; cy < H; ++cy)
        for (int cx = 0; cx < W; ++cx)
            in[cy][cx] = cellIn(cx, cy) ? 1 : 0;

    for (int cy = 0; cy < H; ++cy) {
        for (int cx = 0; cx < W; ++cx) {
            if (!in[cy][cx]) continue;
            ++ref.area;
            // four unit sides; a neighbor cell out of the bounding box is
            // outside the union, hence an exposed border edge.
            if (cx == 0 || !in[cy][cx - 1]) ++ref.perimeter;
            if (cx == W - 1 || !in[cy][cx + 1]) ++ref.perimeter;
            if (cy == 0 || !in[cy - 1][cx]) ++ref.perimeter;
            if (cy == H - 1 || !in[cy + 1][cx]) ++ref.perimeter;
        }
    }
    return ref;
}

void expectMatchesGrid(const std::vector<Rect>& rects,
                       long long expectArea = -1, long long expectPerim = -1) {
    Metrics m = ru::computeUnionMetrics(rects);
    GridRef ref = gridReference(rects);
    EXPECT_EQ(static_cast<long long>(m.area), ref.area);
    EXPECT_EQ(static_cast<long long>(m.perimeter), ref.perimeter);
    if (expectArea >= 0) EXPECT_EQ(static_cast<long long>(m.area), expectArea);
    if (expectPerim >= 0) EXPECT_EQ(static_cast<long long>(m.perimeter), expectPerim);
}

TEST(Sweep, EmptyInput) {
    Metrics m = ru::computeUnionMetrics({});
    EXPECT_EQ(m.area, 0);
    EXPECT_EQ(m.perimeter, 0);
}

TEST(Sweep, SingleRectangle) {
    // [0,2) x [0,2): area 4, perimeter 8
    expectMatchesGrid({{0, 0, 2, 2}}, 4, 8);
}

TEST(Sweep, SingleRectangleNonUnit) {
    expectMatchesGrid({{1, 2, 4, 7}}, 15, 16);  // 3x5
}

TEST(Sweep, IdenticalOverlap) {
    expectMatchesGrid({{0, 0, 2, 2}, {0, 0, 2, 2}}, 4, 8);
}

TEST(Sweep, PartialOverlap) {
    // two 2x2 squares overlap in a 1x1 cell at (1,1): union area 7.
    expectMatchesGrid({{0, 0, 2, 2}, {1, 1, 3, 3}}, 7, 12);
}

TEST(Sweep, EdgeAdjacentX) {
    // share the vertical segment x=2: seam must not be counted.
    expectMatchesGrid({{0, 0, 2, 2}, {2, 0, 4, 2}}, 8, 12);
}

TEST(Sweep, EdgeAdjacentY) {
    // share the horizontal segment y=2.
    expectMatchesGrid({{0, 0, 2, 2}, {0, 2, 2, 4}}, 8, 12);
}

TEST(Sweep, NestedFullyContained) {
    // inner rect lies strictly inside the outer one: area/perim of outer only.
    expectMatchesGrid({{0, 0, 4, 4}, {1, 1, 3, 3}}, 16, 16);
}

TEST(Sweep, NestedTouchingSides) {
    // inner shares two sides with the outer box; still no extra perimeter.
    expectMatchesGrid({{0, 0, 4, 4}, {0, 0, 2, 2}}, 16, 16);
}

TEST(Sweep, ZeroAreaRectsAreIgnored) {
    // vertical segment, horizontal segment, point: all empty under [,) semantics.
    expectMatchesGrid({{2, 0, 2, 4}, {0, 2, 4, 2}, {3, 3, 3, 3}}, 0, 0);
}

TEST(Sweep, ZeroAreaMixedWithReal) {
    expectMatchesGrid({{0, 0, 2, 2}, {2, 2, 2, 2}, {2, 0, 2, 2}}, 4, 8);
}

TEST(Sweep, ZeroWidthTouchingAdjacentRects) {
    // a zero-width box exactly on the shared seam changes nothing.
    expectMatchesGrid({{0, 0, 2, 2}, {2, 0, 4, 2}, {2, 0, 2, 2}}, 8, 12);
}

TEST(Sweep, SameXMultipleEvents) {
    // at x=2: one rect ends, two start; at x=4: two end.
    // layout: A [0,2)x[0,2), B [2,4)x[0,2), C [2,4)x[2,4)
    // union is an L shape of three 2x2 blocks missing top-left: area 12.
    expectMatchesGrid({{0, 0, 2, 2}, {2, 0, 4, 2}, {2, 2, 4, 4}}, 12, 16);
}

TEST(Sweep, SameXStartAndEndTogether) {
    // B starts exactly where A ends while overlapping in y; simultaneously C
    // starts elsewhere. Add-before-remove must not manufacture seam length.
    expectMatchesGrid({{0, 0, 2, 3}, {2, 0, 4, 3}, {2, 3, 4, 6}}, 18, 20);
}

TEST(Sweep, CornerTouchingOnly) {
    // squares touching at the single point (2,2): perimeter is sum, area sum.
    expectMatchesGrid({{0, 0, 2, 2}, {2, 2, 4, 4}}, 8, 16);
}

TEST(Sweep, NegativeCoordinates) {
    expectMatchesGrid({{-2, -2, 0, 0}, {0, 0, 2, 2}}, 8, 16);
}

TEST(Sweep, NegativeOverlapping) {
    // 4x4 and 3x3 overlap in 2x2: 16 + 9 - 4 = 21.
    expectMatchesGrid({{-3, -3, 1, 1}, {-1, -1, 2, 2}}, 21, 20);
}

TEST(Sweep, DiagonalChainAdjacent) {
    // four cells in a staircase; every pair touches only at a point.
    expectMatchesGrid({{0, 0, 1, 1}, {1, 1, 2, 2}, {2, 2, 3, 3}, {3, 3, 4, 4}},
                      4, 16);
}

TEST(Sweep, SurroundedHoleFilled) {
    // ring of four rects around cell (1,1), plus a fifth that fills it:
    // filled union is a 3x3 block.
    std::vector<Rect> ring = {
        {0, 0, 3, 1}, {0, 2, 3, 3}, {0, 1, 1, 2}, {2, 1, 3, 2}, {1, 1, 2, 2}};
    expectMatchesGrid(ring, 9, 12);
}

TEST(Sweep, RingWithoutFill) {
    // same ring WITHOUT the filler: area 8, and the hole adds 4 inner edges.
    std::vector<Rect> ring = {
        {0, 0, 3, 1}, {0, 2, 3, 3}, {0, 1, 1, 2}, {2, 1, 3, 2}};
    expectMatchesGrid(ring, 8, 16);
}

TEST(Sweep, LargeCoordinatesNoOverflow) {
    // half-open 1e9 box: area 1e18 (<= int64 max), perimeter 4e9.
    Rect r{0, 0, 1'000'000'000LL, 1'000'000'000LL};
    Metrics m = ru::computeUnionMetrics({r});
    EXPECT_EQ(m.area, 1'000'000'000'000'000'000LL);
    EXPECT_EQ(m.perimeter, 4'000'000'000LL);
}

// Random differential test against the grid reference. Coordinates stay on a
// 0..7 lattice so the O(grid) reference is cheap; degenerate boxes are
// generated on purpose.
TEST(Sweep, FuzzVsGridReference) {
    std::mt19937 rng(0xC0FFEEu);
    std::uniform_int_distribution<int> coordDist(-2, 6);
    std::uniform_int_distribution<int> countDist(0, 7);

    for (int trial = 0; trial < 5000; ++trial) {
        int n = countDist(rng);
        std::vector<Rect> rects;
        rects.reserve(n);
        for (int k = 0; k < n; ++k) {
            i64 a = coordDist(rng), b = coordDist(rng);
            i64 c = coordDist(rng), d = coordDist(rng);
            // allow equal coords (degenerate); avoid strictly inverted boxes
            rects.push_back({std::min(a, b), std::min(c, d),
                             std::max(a, b), std::max(c, d)});
        }
        Metrics m = ru::computeUnionMetrics(rects);
        GridRef ref = gridReference(rects);
        ASSERT_EQ(static_cast<long long>(m.area), ref.area);
        ASSERT_EQ(static_cast<long long>(m.perimeter), ref.perimeter);
    }
}

// Stress: many identical/adjacent boxes must stay O(n log n) and give the
// block's metrics (adjacency seams cancel).
TEST(Sweep, StressAdjacentStrip) {
    std::vector<Rect> rects;
    for (int k = 0; k < 20000; ++k) rects.push_back({k, 0, k + 1, 3});
    Metrics m = ru::computeUnionMetrics(rects);
    EXPECT_EQ(m.area, 60000);
    EXPECT_EQ(m.perimeter, 40006);
}

}  // namespace
