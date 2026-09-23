#include "geometry.hpp"

#include <algorithm>
#include <cmath>
#include <cstdint>
#include <limits>
#include <set>
#include <utility>

namespace {

// Fast exact orientation when every coordinate fits in int64: the cross
// product is computed with __int128 (never overflows for 64-bit operands).
// Returns +1/-1/0, or INT8_MIN when operands do not fit (caller falls back
// to arbitrary-precision integers).
int orientFast(const Pt& a, const Pt& b, const Pt& c) {
    using I128 = __int128_t;
    auto fits = [](const Int& v) -> bool {
        return v >= std::numeric_limits<int64_t>::min() &&
               v <= std::numeric_limits<int64_t>::max();
    };
    if (!(fits(a.x) && fits(a.y) && fits(b.x) && fits(b.y) && fits(c.x) &&
          fits(c.y)))
        return INT8_MIN;
    int64_t ax = (int64_t)a.x, ay = (int64_t)a.y;
    int64_t bx = (int64_t)b.x, by = (int64_t)b.y;
    int64_t cx = (int64_t)c.x, cy = (int64_t)c.y;
    I128 z = (I128)(bx - ax) * (cy - ay) - (I128)(by - ay) * (cx - ax);
    return z > 0 ? 1 : z < 0 ? -1 : 0;
}

} // namespace

Int cross(const Pt& a, const Pt& b, const Pt& c) {
    return (b.x - a.x) * (c.y - a.y) - (b.y - a.y) * (c.x - a.x);
}

Int ringSignedArea2(const Ring& r) {
    Int s = 0;
    for (size_t i = 0; i + 1 < r.v.size(); ++i) {
        s += r.v[i].x * r.v[i + 1].y - r.v[i + 1].x * r.v[i].y;
    }
    return s;
}

bool onSegment(const Pt& p, const Pt& a, const Pt& b) {
    int o = orientFast(a, b, p);
    if (o == INT8_MIN) {
        if (cross(a, b, p) != 0) return false;
    } else if (o != 0) {
        return false;
    }
    return p.x >= std::min(a.x, b.x) && p.x <= std::max(a.x, b.x) &&
           p.y >= std::min(a.y, b.y) && p.y <= std::max(a.y, b.y);
}

namespace {

// General orientation (exact), using the int64 fast path when possible.
int orient(const Pt& a, const Pt& b, const Pt& c) {
    int f = orientFast(a, b, c);
    if (f != INT8_MIN) return f;
    Int z = cross(a, b, c);
    if (z > 0) return 1;
    if (z < 0) return -1;
    return 0;
}

bool bboxOverlap(const Pt& a, const Pt& b, const Pt& c, const Pt& d) {
    return std::max(std::min(a.x, b.x), std::min(c.x, d.x)) <=
               std::min(std::max(a.x, b.x), std::max(c.x, d.x)) &&
           std::max(std::min(a.y, b.y), std::min(c.y, d.y)) <=
               std::min(std::max(a.y, b.y), std::max(c.y, d.y));
}

bool pointInBox(const Pt& p, const Pt& a, const Pt& b) {
    return p.x >= std::min(a.x, b.x) && p.x <= std::max(a.x, b.x) &&
           p.y >= std::min(a.y, b.y) && p.y <= std::max(a.y, b.y);
}

bool segmentsIntersectGeneral(const Pt& a, const Pt& b, const Pt& c,
                              const Pt& d) {
    int o1 = orient(a, b, c);
    int o2 = orient(a, b, d);
    int o3 = orient(c, d, a);
    int o4 = orient(c, d, b);
    // Proper crossing.
    if (((o1 > 0 && o2 < 0) || (o1 < 0 && o2 > 0)) &&
        ((o3 > 0 && o4 < 0) || (o3 < 0 && o4 > 0)))
        return true;
    // Endpoint-on-segment touches and collinear overlaps (orientation 0 plus
    // bounding-box containment).
    if (o1 == 0 && pointInBox(c, a, b)) return true;
    if (o2 == 0 && pointInBox(d, a, b)) return true;
    if (o3 == 0 && pointInBox(a, c, d)) return true;
    if (o4 == 0 && pointInBox(b, c, d)) return true;
    return false;
}

// Check intersection of two edges of the SAME ring, given whether they are
// adjacent in the cyclic edge order.
bool sameRingEdgesIntersect(const Pt& a, const Pt& b, const Pt& c,
                            const Pt& d, bool adjacent) {
    if (!adjacent) {
        return segmentsIntersectGeneral(a, b, c, d);
    }
    // Adjacent edges share exactly one endpoint, which is legal. Illegal only
    // if they are collinear and overlap beyond that endpoint (backtracking,
    // e.g. a spike that folds along itself).
    if (!bboxOverlap(a, b, c, d)) return false;
    int o1 = orient(a, b, c);
    int o2 = orient(a, b, d);
    if (o1 == 0 && o2 == 0) {
        // Collinear adjacent edges: legal only when they touch at the shared
        // endpoint without overlapping interiors, i.e. they point opposite
        // directions (a 180-degree turn is fine) or one has zero length
        // (zero-length edges were already removed).
        Pt shared;
        if (b == c) shared = b;          // edges a->b and c->d, share b/c
        else if (a == d) shared = a;     // share a/d
        else return true;                // unexpected: treat as illegal
        // Non-overlap iff the other endpoints lie on opposite open sides
        // (strictly) of the shared point along the line.
        Int dot = (a.x - shared.x) * (d.x - shared.x) +
                  (a.y - shared.y) * (d.y - shared.y);
        if (dot >= 0) return true; // same direction => overlap / fold
        return false;
    }
    // Non-collinear adjacent edges only meet at their shared endpoint.
    return false;
}

} // namespace

bool segmentsIntersectShared(const Pt& a, const Pt& b, const Pt& c,
                             const Pt& d, int shared) {
    if (shared < 0) return segmentsIntersectGeneral(a, b, c, d);
    // They are known to share one endpoint. Illegal intersection iff the
    // interiors overlap/cross.
    if (!bboxOverlap(a, b, c, d)) return false;
    int o1 = orient(a, b, c);
    int o2 = orient(a, b, d);
    if (o1 == 0 && o2 == 0) {
        // Collinear with one shared endpoint: overlap iff the other endpoints
        // are on the same side of (or one at) that endpoint.
        Pt sp;
        Pt other1 = a, other2 = c;
        switch (shared) {
            case 0: sp = a; other1 = b; other2 = d; break; // a==c
            case 1: sp = a; other1 = b; other2 = c; break; // a==d
            case 2: sp = b; other1 = a; other2 = d; break; // b==c
            default: sp = b; other1 = a; other2 = c; break; // b==d
        }
        Int dot = (other1.x - sp.x) * (other2.x - sp.x) +
                  (other1.y - sp.y) * (other2.y - sp.y);
        return dot >= 0;
    }
    // Non-collinear segments sharing an endpoint only meet there: legal.
    return false;
}

ValidationResult validateAndNormalizeRing(Ring& r) {
    auto fail = [](const std::string& code, const std::string& msg) {
        return ValidationResult{false, code, msg};
    };

    if (r.v.size() < 3)
        return fail("RING_TOO_SHORT",
                    "ring must contain at least 3 distinct vertices");

    // Close if open.
    if (r.v.front() != r.v.back()) r.v.push_back(r.v.front());

    // Remove consecutive duplicate points (including across the closing seam).
    std::vector<Pt> cleaned;
    for (const Pt& p : r.v) {
        if (cleaned.empty() || cleaned.back() != p) cleaned.push_back(p);
    }
    if (cleaned.size() >= 2 && cleaned.front() == cleaned.back())
        cleaned.pop_back(); // keep open form for now
    if (cleaned.size() < 3)
        return fail("RING_TOO_SHORT",
                    "ring must contain at least 3 distinct vertices");

    // Reject duplicate (non-consecutive) vertices: rings must be simple.
    std::vector<std::pair<std::string, std::string>> keyed;
    keyed.reserve(cleaned.size());
    for (const Pt& p : cleaned)
        keyed.emplace_back(p.x.str(), p.y.str());
    std::sort(keyed.begin(), keyed.end());
    for (size_t i = 1; i < keyed.size(); ++i) {
        if (keyed[i] == keyed[i - 1])
            return fail("DUPLICATE_VERTEX",
                        "ring contains a repeated vertex; only simple rings are accepted");
    }

    // Rotate to start at the lexicographically smallest vertex (deterministic
    // canonical order), then close again.
    size_t start = 0;
    for (size_t i = 1; i < cleaned.size(); ++i) {
        if (cleaned[i].y < cleaned[start].y ||
            (cleaned[i].y == cleaned[start].y &&
             cleaned[i].x < cleaned[start].x)) {
            start = i;
        }
    }
    std::vector<Pt> canon;
    canon.reserve(cleaned.size() + 1);
    for (size_t k = 0; k < cleaned.size(); ++k)
        canon.push_back(cleaned[(start + k) % cleaned.size()]);
    canon.push_back(canon.front());
    r.v = std::move(canon);

    // Pairwise edge intersection test. n edges, adjacent edges share an
    // endpoint legally. Done before the area check: self-intersecting rings
    // (e.g. a bowtie) can also have zero signed area.
    const size_t n = r.v.size() - 1;
    for (size_t i = 0; i < n; ++i) {
        for (size_t j = i + 1; j < n; ++j) {
            bool adjacent = (j == i + 1) || (i == 0 && j == n - 1);
            const Pt& a = r.v[i];
            const Pt& b = r.v[i + 1];
            const Pt& c = r.v[j];
            const Pt& d = r.v[j + 1];
            if (sameRingEdgesIntersect(a, b, c, d, adjacent))
                return fail("SELF_INTERSECTING_RING",
                            "ring edges intersect (only simple rings are accepted)");
        }
    }

    // Zero area (all collinear) is not a valid area ring.
    if (ringSignedArea2(r) == 0)
        return fail("ZERO_AREA_RING", "ring has zero area (degenerate)");
    return ValidationResult{true, "", ""};
}

ValidationResult validatePolygon(Polygon& poly) {
    auto fail = [](const std::string& code, const std::string& msg) {
        return ValidationResult{false, code, msg};
    };

    // Canonicalize orientation: outer CCW (positive area2), holes CW.
    auto reverseOpen = [](Ring& rng) {
        std::vector<Pt> open(rng.v.begin(), rng.v.end() - 1);
        std::reverse(open.begin(), open.end());
        open.push_back(open.front());
        rng.v = std::move(open);
    };
    if (ringSignedArea2(poly.outer) < 0) reverseOpen(poly.outer);
    if (ringSignedArea2(poly.outer) == 0)
        return fail("ZERO_AREA_RING", "outer ring has zero area");

    for (Ring& h : poly.holes) {
        if (ringSignedArea2(h) > 0) reverseOpen(h);
        if (ringSignedArea2(h) == 0)
            return fail("ZERO_AREA_RING", "hole has zero area");
    }

    // Every hole vertex must be strictly inside the outer ring.
    for (size_t hi = 0; hi < poly.holes.size(); ++hi) {
        for (const Pt& p : poly.holes[hi].v) {
            Rel r = locateNaiveRing(poly.outer, p);
            if (r != Rel::Inside)
                return fail("HOLE_OUTSIDE_OUTER",
                            "hole " + std::to_string(hi) +
                                " has a vertex outside or on the outer ring");
        }
    }

    // Every outer vertex must be strictly outside each hole (cannot touch).
    for (size_t hi = 0; hi < poly.holes.size(); ++hi) {
        for (const Pt& p : poly.outer.v) {
            if (locateNaiveRing(poly.holes[hi], p) != Rel::Outside)
                return fail("OUTER_TOUCHES_HOLE",
                            "outer ring vertex lies inside or on hole " +
                                std::to_string(hi));
        }
    }

    // Pairs of holes: edges must not cross or touch, interiors must be
    // disjoint (no nesting).
    for (size_t i = 0; i < poly.holes.size(); ++i) {
        for (size_t j = i + 1; j < poly.holes.size(); ++j) {
            const Ring& a = poly.holes[i];
            const Ring& b = poly.holes[j];

            // Edge crossings/touches first (covers T-junctions, collinear
            // overlaps and crossings with no vertex inside the other ring).
            size_t na = a.v.size() - 1, nb = b.v.size() - 1;
            for (size_t ea = 0; ea < na; ++ea)
                for (size_t eb = 0; eb < nb; ++eb)
                    if (segmentsIntersectShared(a.v[ea], a.v[ea + 1], b.v[eb],
                                                b.v[eb + 1], -1))
                        return fail("INTERSECTING_HOLES",
                                    "holes " + std::to_string(i) + " and " +
                                        std::to_string(j) + " touch or cross");

            // Vertex/ring relations for the nesting case.
            bool aInsideB = false, bInsideA = false;
            for (const Pt& p : a.v)
                if (locateNaiveRing(b, p) == Rel::Inside) aInsideB = true;
            for (const Pt& p : b.v)
                if (locateNaiveRing(a, p) == Rel::Inside) bInsideA = true;
            if (aInsideB || bInsideA)
                return fail("NESTED_HOLES",
                            "holes " + std::to_string(i) + " and " +
                                std::to_string(j) + " are nested");
        }
    }
    return ValidationResult{true, "", ""};
}

const char* relName(Rel r) {
    switch (r) {
        case Rel::Inside: return "inside";
        case Rel::Outside: return "outside";
        case Rel::Bound: return "boundary";
    }
    return "unknown";
}

Rel locateNaiveRing(const Ring& r, const Pt& p) {
    // 1. Boundary test.
    for (size_t i = 0; i + 1 < r.v.size(); ++i) {
        if (onSegment(p, r.v[i], r.v[i + 1])) return Rel::Bound;
    }
    // 2. Parity with half-open rule: edge counts iff (y0 <= py < y1) where
    //    y0 = min(vy_i, vy_j), y1 = max(...). Horizontal edges never count.
    bool inside = false;
    for (size_t i = 0; i + 1 < r.v.size(); ++i) {
        const Pt& a = r.v[i];
        const Pt& b = r.v[i + 1];
        const Pt& lo = (a.y <= b.y) ? a : b;
        const Pt& hi = (a.y <= b.y) ? b : a;
        if (lo.y == hi.y) continue;          // horizontal
        if (!(lo.y <= p.y && p.y < hi.y)) continue;
        // x intersection of the supporting line with y = p.y, compared with p.x.
        // (xint - p.x) sign via cross product: edge a->b, point p.
        Int z = cross(a, b, p);
        // Ray from p toward -infinity (left) crosses this edge iff the
        // intersection is strictly to the RIGHT of p: x_int > p.x.
        // For upward a->b, cross(a,b,p) < 0 means p is right of the directed
        // edge, i.e. x_int > p.x.
        if (a.y < b.y) {
            if (z < 0) inside = !inside;
        } else {
            if (z > 0) inside = !inside;
        }
    }
    return inside ? Rel::Inside : Rel::Outside;
}

Rel locateNaivePolygon(const Polygon& poly, const Pt& p) {
    Rel r = locateNaiveRing(poly.outer, p);
    if (r == Rel::Outside) return Rel::Outside;
    if (r == Rel::Bound) return Rel::Bound;
    for (const Ring& h : poly.holes) {
        Rel hr = locateNaiveRing(h, p);
        if (hr == Rel::Bound) return Rel::Bound;
        if (hr == Rel::Inside) return Rel::Outside;
    }
    return Rel::Inside;
}
