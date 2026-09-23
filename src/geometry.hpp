// Exact planar geometry on integer coordinates.
//
// Coordinates are int64_t. Every orientation test uses signed 128-bit
// intermediate products (__int128), so predicates are exact: no epsilon,
// no floating-point rounding. Input decimals are scaled to integers by the
// request layer (see decimal.hpp); coordinate limits are documented there.
#pragma once

#include <cstdint>
#include <string>
#include <vector>

namespace geo {

using i64 = int64_t;
using u64 = uint64_t;
using i128 = __int128_t;
using u128 = __uint128_t;

struct Point {
    i64 x = 0;
    i64 y = 0;
};

enum class Location { Outside = -1, Boundary = 0, Inside = 1 };

// Exact orientation of triangle (a, b, p): >0 left turn, <0 right turn.
i128 orient(const Point& a, const Point& b, const Point& p);

// True if p lies on segment ab (endpoints included). Exact.
bool pointOnSegment(const Point& p, const Point& a, const Point& b);

// Horizontal-ray crossing test for one ring, boundary checked first.
// The ray runs to +x; rings need not be oriented. Returns Location.
//
// Degenerate vertex cases (horizontal ray through a vertex) are handled by
// the half-open-edge rule: an edge counts only when exactly one endpoint is
// strictly above the ray (lower endpoint inclusive, upper exclusive).
// Flat edges never cross.
Location pointInRing(const Point& p, const std::vector<Point>& ring);

// For an edge (a,b) already known to straddle the ray y=p.y (half-open
// rule), return true iff the edge/ray intersection is strictly right of p.
// Exact; valid for both upward and downward edges (the comparison uses the
// sign of (b.y-a.y), never division).
bool rayCrossesRight(const Point& p, const Point& a, const Point& b);

// ----- 256-bit signed magnitude accumulator -----------------------------
// Needed for signed area of large rings: sum of up to ~20000 products of
// values < 2^62 can reach ~2^139, which overflows 128 bits.
struct ScaledSum256 {
    bool neg = false;
    u128 lo = 0; // bits [0,128)
    u128 hi = 0; // bits [128,256)

    void addTerm(u128 magnitude, bool termNegative);
    int sign() const; // -1 / 0 / 1
};

// Signed area sign of a ring (>0 CCW, <0 CW, 0 degenerate). Exact.
int ringAreaSign(const std::vector<Point>& ring);

// ----- Ring validation ---------------------------------------------------

enum class RingErrorKind {
    DuplicatePoint,      // consecutive duplicate (after closing)
    TooFewPoints,        // < 3 distinct vertices
    DegenerateArea,      // collinear / zero signed area
    SelfIntersection,    // proper crossing, vertex touch, or edge overlap
};

struct RingError {
    RingErrorKind kind;
    int edgeIndex = -1; // first edge involved, when applicable
    std::string detail;
};

// Returns the first validity problem found, or nullptr.
// A valid ring: >=3 distinct vertices, no consecutive duplicates, nonzero
// signed area, and no two non-adjacent edges cross/touch/overlap.
// Adjacent edges may meet only at their shared vertex (spikes rejected).
bool validateRing(const std::vector<Point>& ring, RingError& err);

// Strict crossing of the interiors of ab and cd.
bool segmentsProperlyCross(const Point& a, const Point& b,
                           const Point& c, const Point& d);

} // namespace geo
