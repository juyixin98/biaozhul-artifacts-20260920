#include "geometry.hpp"

#include <algorithm>
#include <cmath>

namespace geo {

i128 orient(const Point& a, const Point& b, const Point& p) {
    // (b-a) x (p-a)
    i128 dx1 = static_cast<i128>(b.x) - a.x;
    i128 dy1 = static_cast<i128>(b.y) - a.y;
    i128 dx2 = static_cast<i128>(p.x) - a.x;
    i128 dy2 = static_cast<i128>(p.y) - a.y;
    return dx1 * dy2 - dy1 * dx2;
}

namespace {

inline int sgn128(i128 v) {
    return (v > 0) - (v < 0);
}

inline bool between1(i64 a, i64 b, i64 v) {
    return v >= std::min(a, b) && v <= std::max(a, b);
}

} // namespace

bool pointOnSegment(const Point& p, const Point& a, const Point& b) {
    if (orient(a, b, p) != 0) return false;
    return between1(a.x, b.x, p.x) && between1(a.y, b.y, p.y);
}

bool rayCrossesRight(const Point& p, const Point& a, const Point& b) {
    // Does the straddling edge ab meet the ray line y=p.y strictly to the
    // right of p? Let DX=b.x-a.x, DY=b.y-a.y, Z=p.y-a.y, W=p.x-a.x.
    //   xint - p.x = (DX*Z - DY*W) / DY
    // (caller guarantees DY != 0 and the straddle, i.e. 0 <= Z/DY < 1).
    // If DY > 0 require DX*Z - DY*W > 0; if DY < 0 require it < 0.
    // Unified: (DX*Z > DY*W) == (DY > 0).
    // Every product fits in signed 128 bits for the documented limits.
    i128 dx = static_cast<i128>(b.x) - a.x;
    i128 dy = static_cast<i128>(b.y) - a.y;
    i128 z = static_cast<i128>(p.y) - a.y;
    i128 w = static_cast<i128>(p.x) - a.x;
    bool positive = dx * z > dy * w;
    return dy > 0 ? positive : !positive;
}

Location pointInRing(const Point& p, const std::vector<Point>& ring) {
    const size_t n = ring.size();
    // Boundary first: exact collinearity + box containment.
    for (size_t i = 0; i < n; ++i) {
        const Point& a = ring[i];
        const Point& b = ring[(i + 1) % n];
        if (pointOnSegment(p, a, b)) return Location::Boundary;
    }

    // Parity of +x ray crossings using half-open edges.
    int crossings = 0;
    for (size_t i = 0; i < n; ++i) {
        const Point& a = ring[i];
        const Point& b = ring[(i + 1) % n];
        // Standard half-open rule: an edge straddles y=p.y iff exactly one
        // endpoint is strictly above p.y (lower endpoint inclusive, upper
        // exclusive). A local minimum counts twice, a local maximum zero,
        // so a ray through a vertex never corrupts parity.
        bool straddles =
            (a.y <= p.y && b.y > p.y) || (b.y <= p.y && a.y > p.y);
        if (!straddles) continue; // flat edges and non-straddling edges skip
        // Collinear support line: p, a, b on one line. Boundary was already
        // checked and returned above, so p is outside the segment; the edge
        // does not contribute a crossing (avoids double counting when the
        // ray runs along a flat extension and meets two straddling edges).
        if (orient(a, b, p) == 0) continue;
        if (rayCrossesRight(p, a, b)) ++crossings;
    }
    return (crossings & 1) ? Location::Inside : Location::Outside;
}

// ----- 256-bit magnitude arithmetic --------------------------------------

namespace {

// dst += m, where dst is a 256-bit unsigned number held in (hi, lo).
void add256(u128& hi, u128& lo, u128 m) {
    u128 newLo = lo + m;
    u128 carry = static_cast<u128>(newLo < lo);
    lo = newLo;
    hi += carry;
}

int cmp256(u128 ahi, u128 alo, u128 bhi, u128 blo) {
    if (ahi != bhi) return ahi > bhi ? 1 : -1;
    if (alo != blo) return alo > blo ? 1 : -1;
    return 0;
}

} // namespace

void ScaledSum256::addTerm(u128 magnitude, bool termNegative) {
    // Split accumulator into current positive (P) and negative (N) totals.
    u128 phi = neg ? 0 : hi;
    u128 plo = neg ? 0 : lo;
    u128 nhi = neg ? hi : 0;
    u128 nlo = neg ? lo : 0;
    if (termNegative) add256(nhi, nlo, magnitude);
    else add256(phi, plo, magnitude);
    int c = cmp256(phi, plo, nhi, nlo);
    if (c == 0) {
        neg = false; hi = 0; lo = 0;
    } else if (c > 0) {
        // positive stays larger: P - N
        // Borrowing subtraction of (nlo..nhi) from (plo..phi).
        u128 rlo = plo - nlo;
        u128 borrow = static_cast<u128>(rlo > plo);
        u128 rhi = phi - nhi - borrow;
        neg = false; hi = rhi; lo = rlo;
    } else {
        u128 rlo = nlo - plo;
        u128 borrow = static_cast<u128>(rlo > nlo);
        u128 rhi = nhi - phi - borrow;
        neg = true; hi = rhi; lo = rlo;
    }
}

int ScaledSum256::sign() const {
    if (hi == 0 && lo == 0) return 0;
    return neg ? -1 : 1;
}

int ringAreaSign(const std::vector<Point>& ring) {
    // Translate by ring[0] to bound |term| < 2^124.
    ScaledSum256 sum;
    const i128 x0 = ring[0].x;
    const i128 y0 = ring[0].y;
    for (size_t i = 1; i + 1 < ring.size(); ++i) {
        i128 ax = static_cast<i128>(ring[i].x) - x0;
        i128 ay = static_cast<i128>(ring[i].y) - y0;
        i128 bx = static_cast<i128>(ring[i + 1].x) - x0;
        i128 by = static_cast<i128>(ring[i + 1].y) - y0;
        i128 term = ax * by - ay * bx;
        sum.addTerm(term < 0 ? static_cast<u128>(-term)
                             : static_cast<u128>(term),
                    term < 0);
    }
    return sum.sign();
}

bool segmentsProperlyCross(const Point& a, const Point& b,
                           const Point& c, const Point& d) {
    int s1 = sgn128(orient(a, b, c));
    int s2 = sgn128(orient(a, b, d));
    int s3 = sgn128(orient(c, d, a));
    int s4 = sgn128(orient(c, d, b));
    return ((s1 > 0 && s2 < 0) || (s1 < 0 && s2 > 0)) &&
           ((s3 > 0 && s4 < 0) || (s3 < 0 && s4 > 0));
}

namespace {

bool properOrTouch(const Point& a, const Point& b,
                   const Point& c, const Point& d) {
    if (segmentsProperlyCross(a, b, c, d)) return true;
    // Touching or overlapping in any form.
    return pointOnSegment(a, c, d) || pointOnSegment(b, c, d) ||
           pointOnSegment(c, a, b) || pointOnSegment(d, a, b);
}

} // namespace

bool validateRing(const std::vector<Point>& ring, RingError& err) {
    const size_t n = ring.size();

    if (n < 3) {
        err.kind = RingErrorKind::TooFewPoints;
        err.detail = "ring must contain at least 3 vertices";
        return false;
    }

    // Consecutive duplicate points, including the closing edge.
    for (size_t i = 0; i < n; ++i) {
        const Point& a = ring[i];
        const Point& b = ring[(i + 1) % n];
        if (a.x == b.x && a.y == b.y) {
            err.kind = RingErrorKind::DuplicatePoint;
            err.edgeIndex = static_cast<int>(i);
            err.detail = "consecutive duplicate vertex";
            return false;
        }
    }

    // Self-intersection is checked before the zero-area test: a self-
    // crossing ring (e.g. an hourglass with two equal opposite lobes) can
    // also have signed area zero, and should be reported as self-crossing.
    // Adjacent edges: reject spikes (one endpoint of either edge lies on
    // the other edge); proper crossings between adjacent edges are
    // impossible except via the shared vertex.
    for (size_t i = 0; i < n; ++i) {
        const Point& a = ring[i];
        const Point& b = ring[(i + 1) % n];
        const Point& c = ring[(i + 2) % n];
        // Segment ab and bc share b; a!=b and b!=c. Collinear overlap occurs
        // iff a lies on bc or c lies on ab (zero-angle reversal / spike).
        if (pointOnSegment(a, b, c) || pointOnSegment(c, a, b)) {
            err.kind = RingErrorKind::SelfIntersection;
            err.edgeIndex = static_cast<int>(i);
            err.detail = "spike: adjacent edges overlap collinearly";
            return false;
        }
    }

    // Non-adjacent edge pairs: no crossing, touch, or overlap at all.
    for (size_t i = 0; i < n; ++i) {
        const Point& a = ring[i];
        const Point& b = ring[(i + 1) % n];
        for (size_t j = i + 1; j < n; ++j) {
            // Edges i and j share a vertex iff j == i+1, or (i,j)==(0,n-1).
            size_t diff = j - i;
            if (diff == 1 || (i == 0 && j == n - 1)) continue;
            const Point& c = ring[j];
            const Point& d = ring[(j + 1) % n];
            if (properOrTouch(a, b, c, d)) {
                err.kind = RingErrorKind::SelfIntersection;
                err.edgeIndex = static_cast<int>(i);
                err.detail = "non-adjacent edges cross or touch";
                return false;
            }
        }
    }

    // With no self-crossing, zero signed area means all points are
    // collinear (a degenerate line, not a 2-D polygon).
    if (ringAreaSign(ring) == 0) {
        err.kind = RingErrorKind::DegenerateArea;
        err.detail = "ring has zero signed area (all points collinear)";
        return false;
    }

    return true;
}

} // namespace geo
